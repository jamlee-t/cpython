import io
import ntpath
import operator
import os
import posixpath
import sys
from errno import *
from glob import _StringGlobber, _no_recurse_symlinks
from itertools import chain
from stat import S_IMODE, S_ISDIR, S_ISREG, S_ISSOCK, S_ISBLK, S_ISCHR, S_ISFIFO
from _collections_abc import Sequence

try:
    import pwd
except ImportError:
    pwd = None
try:
    import grp
except ImportError:
    grp = None

from pathlib._os import copyfile, PathInfo, DirEntryInfo
from pathlib._abc import CopyReader, CopyWriter, JoinablePath, ReadablePath, WritablePath


__all__ = [
    "UnsupportedOperation",
    "PurePath", "PurePosixPath", "PureWindowsPath",
    "Path", "PosixPath", "WindowsPath",
    ]


class UnsupportedOperation(NotImplementedError):
    """An exception that is raised when an unsupported operation is attempted.
    """
    pass


class _PathParents(Sequence):
    """This object provides sequence-like access to the logical ancestors
    of a path.  Don't try to construct it yourself."""
    __slots__ = ('_path', '_drv', '_root', '_tail')

    def __init__(self, path):
        self._path = path
        self._drv = path.drive
        self._root = path.root
        self._tail = path._tail

    def __len__(self):
        return len(self._tail)

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return tuple(self[i] for i in range(*idx.indices(len(self))))

        if idx >= len(self) or idx < -len(self):
            raise IndexError(idx)
        if idx < 0:
            idx += len(self)
        return self._path._from_parsed_parts(self._drv, self._root,
                                             self._tail[:-idx - 1])

    def __repr__(self):
        return "<{}.parents>".format(type(self._path).__name__)


class _LocalCopyReader(CopyReader):
    """This object implements the "read" part of copying local paths. Don't
    try to construct it yourself.
    """
    __slots__ = ()

    _readable_metakeys = {'mode', 'times_ns'}
    if hasattr(os.stat_result, 'st_flags'):
        _readable_metakeys.add('flags')
    if hasattr(os, 'listxattr'):
        _readable_metakeys.add('xattrs')
    _readable_metakeys = frozenset(_readable_metakeys)

    def _read_metadata(self, metakeys, *, follow_symlinks=True):
        metadata = {}
        if 'mode' in metakeys or 'times_ns' in metakeys or 'flags' in metakeys:
            st = self._path.stat(follow_symlinks=follow_symlinks)
            if 'mode' in metakeys:
                metadata['mode'] = S_IMODE(st.st_mode)
            if 'times_ns' in metakeys:
                metadata['times_ns'] = st.st_atime_ns, st.st_mtime_ns
            if 'flags' in metakeys:
                metadata['flags'] = st.st_flags
        if 'xattrs' in metakeys:
            try:
                metadata['xattrs'] = [
                    (attr, os.getxattr(self._path, attr, follow_symlinks=follow_symlinks))
                    for attr in os.listxattr(self._path, follow_symlinks=follow_symlinks)]
            except OSError as err:
                if err.errno not in (EPERM, ENOTSUP, ENODATA, EINVAL, EACCES):
                    raise
        return metadata


class _LocalCopyWriter(CopyWriter):
    """This object implements the "write" part of copying local paths. Don't
    try to construct it yourself.
    """
    __slots__ = ()

    _writable_metakeys = _LocalCopyReader._readable_metakeys

    def _write_metadata(self, metadata, *, follow_symlinks=True):
        def _nop(*args, ns=None, follow_symlinks=None):
            pass

        if follow_symlinks:
            # use the real function if it exists
            def lookup(name):
                return getattr(os, name, _nop)
        else:
            # use the real function only if it exists
            # *and* it supports follow_symlinks
            def lookup(name):
                fn = getattr(os, name, _nop)
                if fn in os.supports_follow_symlinks:
                    return fn
                return _nop

        times_ns = metadata.get('times_ns')
        if times_ns is not None:
            lookup("utime")(self._path, ns=times_ns, follow_symlinks=follow_symlinks)
        # We must copy extended attributes before the file is (potentially)
        # chmod()'ed read-only, otherwise setxattr() will error with -EACCES.
        xattrs = metadata.get('xattrs')
        if xattrs is not None:
            for attr, value in xattrs:
                try:
                    os.setxattr(self._path, attr, value, follow_symlinks=follow_symlinks)
                except OSError as e:
                    if e.errno not in (EPERM, ENOTSUP, ENODATA, EINVAL, EACCES):
                        raise
        mode = metadata.get('mode')
        if mode is not None:
            try:
                lookup("chmod")(self._path, mode, follow_symlinks=follow_symlinks)
            except NotImplementedError:
                # if we got a NotImplementedError, it's because
                #   * follow_symlinks=False,
                #   * lchown() is unavailable, and
                #   * either
                #       * fchownat() is unavailable or
                #       * fchownat() doesn't implement AT_SYMLINK_NOFOLLOW.
                #         (it returned ENOSUP.)
                # therefore we're out of options--we simply cannot chown the
                # symlink.  give up, suppress the error.
                # (which is what shutil always did in this circumstance.)
                pass
        flags = metadata.get('flags')
        if flags is not None:
            try:
                lookup("chflags")(self._path, flags, follow_symlinks=follow_symlinks)
            except OSError as why:
                if why.errno not in (EOPNOTSUPP, ENOTSUP):
                    raise

    if copyfile:
        # Use fast OS routine for local file copying where available.
        def _create_file(self, source, metakeys):
            """Copy the given file to the given target."""
            try:
                source = os.fspath(source)
            except TypeError:
                if not isinstance(source, WritablePath):
                    raise
                super()._create_file(source, metakeys)
            else:
                copyfile(source, os.fspath(self._path))

    if os.name == 'nt':
        # Windows: symlink target might not exist yet if we're copying several
        # files, so ensure we pass is_dir to os.symlink().
        def _create_symlink(self, source, metakeys):
            """Copy the given symlink to the given target."""
            self._path.symlink_to(source.readlink(), source.is_dir())
            if metakeys:
                metadata = source._copy_reader._read_metadata(metakeys, follow_symlinks=False)
                if metadata:
                    self._write_metadata(metadata, follow_symlinks=False)

    def _ensure_different_file(self, source):
        """
        Raise OSError(EINVAL) if both paths refer to the same file.
        """
        try:
            if not self._path.samefile(source):
                return
        except (OSError, ValueError):
            return
        err = OSError(EINVAL, "Source and target are the same file")
        err.filename = str(source)
        err.filename2 = str(self._path)
        raise err


class PurePath(JoinablePath):
    """Base class for manipulating paths without I/O.

    PurePath represents a filesystem path and offers operations which
    don't imply any actual filesystem I/O.  Depending on your system,
    instantiating a PurePath will return either a PurePosixPath or a
    PureWindowsPath object.  You can also instantiate either of these classes
    directly, regardless of your system.
    """

    __slots__ = (
        # The `_raw_paths` slot stores unjoined string paths. This is set in
        # the `__init__()` method.
        '_raw_paths',

        # The `_drv`, `_root` and `_tail_cached` slots store parsed and
        # normalized parts of the path. They are set when any of the `drive`,
        # `root` or `_tail` properties are accessed for the first time. The
        # three-part division corresponds to the result of
        # `os.path.splitroot()`, except that the tail is further split on path
        # separators (i.e. it is a list of strings), and that the root and
        # tail are normalized.
        '_drv', '_root', '_tail_cached',

        # The `_str` slot stores the string representation of the path,
        # computed from the drive, root and tail when `__str__()` is called
        # for the first time. It's used to implement `_str_normcase`
        '_str',

        # The `_str_normcase_cached` slot stores the string path with
        # normalized case. It is set when the `_str_normcase` property is
        # accessed for the first time. It's used to implement `__eq__()`
        # `__hash__()`, and `_parts_normcase`
        '_str_normcase_cached',

        # The `_parts_normcase_cached` slot stores the case-normalized
        # string path after splitting on path separators. It's set when the
        # `_parts_normcase` property is accessed for the first time. It's used
        # to implement comparison methods like `__lt__()`.
        '_parts_normcase_cached',

        # The `_hash` slot stores the hash of the case-normalized string
        # path. It's set when `__hash__()` is called for the first time.
        '_hash',
    )
    parser = os.path

    def __new__(cls, *args, **kwargs):
        """Construct a PurePath from one or several strings and or existing
        PurePath objects.  The strings and path objects are combined so as
        to yield a canonicalized path, which is incorporated into the
        new PurePath object.
        """
        if cls is PurePath:
            cls = PureWindowsPath if os.name == 'nt' else PurePosixPath
        return object.__new__(cls)

    def __init__(self, *args):
        paths = []
        for arg in args:
            if isinstance(arg, PurePath):
                if arg.parser is not self.parser:
                    # GH-103631: Convert separators for backwards compatibility.
                    paths.append(arg.as_posix())
                else:
                    paths.extend(arg._raw_paths)
            else:
                try:
                    path = os.fspath(arg)
                except TypeError:
                    path = arg
                if not isinstance(path, str):
                    raise TypeError(
                        "argument should be a str or an os.PathLike "
                        "object where __fspath__ returns a str, "
                        f"not {type(path).__name__!r}")
                paths.append(path)
        self._raw_paths = paths

    def with_segments(self, *pathsegments):
        """Construct a new path object from any number of path-like objects.
        Subclasses may override this method to customize how new path objects
        are created from methods like `iterdir()`.
        """
        return type(self)(*pathsegments)

    def joinpath(self, *pathsegments):
        """Combine this path with one or several arguments, and return a
        new path representing either a subpath (if all arguments are relative
        paths) or a totally different path (if one of the arguments is
        anchored).
        """
        return self.with_segments(self, *pathsegments)

    def __truediv__(self, key):
        try:
            return self.with_segments(self, key)
        except TypeError:
            return NotImplemented

    def __rtruediv__(self, key):
        try:
            return self.with_segments(key, self)
        except TypeError:
            return NotImplemented

    def __reduce__(self):
        return self.__class__, tuple(self._raw_paths)

    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, self.as_posix())

    def __fspath__(self):
        return str(self)

    def __bytes__(self):
        """Return the bytes representation of the path.  This is only
        recommended to use under Unix."""
        return os.fsencode(self)

    @property
    def _str_normcase(self):
        # String with normalized case, for hashing and equality checks
        try:
            return self._str_normcase_cached
        except AttributeError:
            if self.parser is posixpath:
                self._str_normcase_cached = str(self)
            else:
                self._str_normcase_cached = str(self).lower()
            return self._str_normcase_cached

    def __hash__(self):
        try:
            return self._hash
        except AttributeError:
            self._hash = hash(self._str_normcase)
            return self._hash

    def __eq__(self, other):
        if not isinstance(other, PurePath):
            return NotImplemented
        return self._str_normcase == other._str_normcase and self.parser is other.parser

    @property
    def _parts_normcase(self):
        # Cached parts with normalized case, for comparisons.
        try:
            return self._parts_normcase_cached
        except AttributeError:
            self._parts_normcase_cached = self._str_normcase.split(self.parser.sep)
            return self._parts_normcase_cached

    def __lt__(self, other):
        if not isinstance(other, PurePath) or self.parser is not other.parser:
            return NotImplemented
        return self._parts_normcase < other._parts_normcase

    def __le__(self, other):
        if not isinstance(other, PurePath) or self.parser is not other.parser:
            return NotImplemented
        return self._parts_normcase <= other._parts_normcase

    def __gt__(self, other):
        if not isinstance(other, PurePath) or self.parser is not other.parser:
            return NotImplemented
        return self._parts_normcase > other._parts_normcase

    def __ge__(self, other):
        if not isinstance(other, PurePath) or self.parser is not other.parser:
            return NotImplemented
        return self._parts_normcase >= other._parts_normcase

    def __str__(self):
        """Return the string representation of the path, suitable for
        passing to system calls."""
        try:
            return self._str
        except AttributeError:
            self._str = self._format_parsed_parts(self.drive, self.root,
                                                  self._tail) or '.'
            return self._str

    @classmethod
    def _format_parsed_parts(cls, drv, root, tail):
        if drv or root:
            return drv + root + cls.parser.sep.join(tail)
        elif tail and cls.parser.splitdrive(tail[0])[0]:
            tail = ['.'] + tail
        return cls.parser.sep.join(tail)

    def _from_parsed_parts(self, drv, root, tail):
        path = self._from_parsed_string(self._format_parsed_parts(drv, root, tail))
        path._drv = drv
        path._root = root
        path._tail_cached = tail
        return path

    def _from_parsed_string(self, path_str):
        path = self.with_segments(path_str)
        path._str = path_str or '.'
        return path

    @classmethod
    def _parse_path(cls, path):
        if not path:
            return '', '', []
        sep = cls.parser.sep
        altsep = cls.parser.altsep
        if altsep:
            path = path.replace(altsep, sep)
        drv, root, rel = cls.parser.splitroot(path)
        if not root and drv.startswith(sep) and not drv.endswith(sep):
            drv_parts = drv.split(sep)
            if len(drv_parts) == 4 and drv_parts[2] not in '?.':
                # e.g. //server/share
                root = sep
            elif len(drv_parts) == 6:
                # e.g. //?/unc/server/share
                root = sep
        return drv, root, [x for x in rel.split(sep) if x and x != '.']

    @classmethod
    def _parse_pattern(cls, pattern):
        """Parse a glob pattern to a list of parts. This is much like
        _parse_path, except:

        - Rather than normalizing and returning the drive and root, we raise
          NotImplementedError if either are present.
        - If the path has no real parts, we raise ValueError.
        - If the path ends in a slash, then a final empty part is added.
        """
        drv, root, rel = cls.parser.splitroot(pattern)
        if root or drv:
            raise NotImplementedError("Non-relative patterns are unsupported")
        sep = cls.parser.sep
        altsep = cls.parser.altsep
        if altsep:
            rel = rel.replace(altsep, sep)
        parts = [x for x in rel.split(sep) if x and x != '.']
        if not parts:
            raise ValueError(f"Unacceptable pattern: {str(pattern)!r}")
        elif rel.endswith(sep):
            # GH-65238: preserve trailing slash in glob patterns.
            parts.append('')
        return parts

    def as_posix(self):
        """Return the string representation of the path with forward (/)
        slashes."""
        return str(self).replace(self.parser.sep, '/')

    @property
    def _raw_path(self):
        paths = self._raw_paths
        if len(paths) == 1:
            return paths[0]
        elif paths:
            # Join path segments from the initializer.
            path = self.parser.join(*paths)
            # Cache the joined path.
            paths.clear()
            paths.append(path)
            return path
        else:
            paths.append('')
            return ''

    @property
    def drive(self):
        """The drive prefix (letter or UNC path), if any."""
        try:
            return self._drv
        except AttributeError:
            self._drv, self._root, self._tail_cached = self._parse_path(self._raw_path)
            return self._drv

    @property
    def root(self):
        """The root of the path, if any."""
        try:
            return self._root
        except AttributeError:
            self._drv, self._root, self._tail_cached = self._parse_path(self._raw_path)
            return self._root

    @property
    def _tail(self):
        try:
            return self._tail_cached
        except AttributeError:
            self._drv, self._root, self._tail_cached = self._parse_path(self._raw_path)
            return self._tail_cached

    @property
    def anchor(self):
        """The concatenation of the drive and root, or ''."""
        return self.drive + self.root

    @property
    def parts(self):
        """An object providing sequence-like access to the
        components in the filesystem path."""
        if self.drive or self.root:
            return (self.drive + self.root,) + tuple(self._tail)
        else:
            return tuple(self._tail)

    @property
    def parent(self):
        """The logical parent of the path."""
        drv = self.drive
        root = self.root
        tail = self._tail
        if not tail:
            return self
        return self._from_parsed_parts(drv, root, tail[:-1])

    @property
    def parents(self):
        """A sequence of this path's logical parents."""
        # The value of this property should not be cached on the path object,
        # as doing so would introduce a reference cycle.
        return _PathParents(self)

    @property
    def name(self):
        """The final path component, if any."""
        tail = self._tail
        if not tail:
            return ''
        return tail[-1]

    def with_name(self, name):
        """Return a new path with the file name changed."""
        p = self.parser
        if not name or p.sep in name or (p.altsep and p.altsep in name) or name == '.':
            raise ValueError(f"Invalid name {name!r}")
        tail = self._tail.copy()
        if not tail:
            raise ValueError(f"{self!r} has an empty name")
        tail[-1] = name
        return self._from_parsed_parts(self.drive, self.root, tail)

    @property
    def stem(self):
        """The final path component, minus its last suffix."""
        name = self.name
        i = name.rfind('.')
        if i != -1:
            stem = name[:i]
            # Stem must contain at least one non-dot character.
            if stem.lstrip('.'):
                return stem
        return name

    @property
    def suffix(self):
        """
        The final component's last suffix, if any.

        This includes the leading period. For example: '.txt'
        """
        name = self.name.lstrip('.')
        i = name.rfind('.')
        if i != -1:
            return name[i:]
        return ''

    @property
    def suffixes(self):
        """
        A list of the final component's suffixes, if any.

        These include the leading periods. For example: ['.tar', '.gz']
        """
        return ['.' + ext for ext in self.name.lstrip('.').split('.')[1:]]

    def relative_to(self, other, *, walk_up=False):
        """Return the relative path to another path identified by the passed
        arguments.  If the operation is not possible (because this is not
        related to the other path), raise ValueError.

        The *walk_up* parameter controls whether `..` may be used to resolve
        the path.
        """
        if not isinstance(other, PurePath):
            other = self.with_segments(other)
        for step, path in enumerate(chain([other], other.parents)):
            if path == self or path in self.parents:
                break
            elif not walk_up:
                raise ValueError(f"{str(self)!r} is not in the subpath of {str(other)!r}")
            elif path.name == '..':
                raise ValueError(f"'..' segment in {str(other)!r} cannot be walked")
        else:
            raise ValueError(f"{str(self)!r} and {str(other)!r} have different anchors")
        parts = ['..'] * step + self._tail[len(path._tail):]
        return self._from_parsed_parts('', '', parts)

    def is_relative_to(self, other):
        """Return True if the path is relative to another path or False.
        """
        if not isinstance(other, PurePath):
            other = self.with_segments(other)
        return other == self or other in self.parents

    def is_absolute(self):
        """True if the path is absolute (has both a root and, if applicable,
        a drive)."""
        if self.parser is posixpath:
            # Optimization: work with raw paths on POSIX.
            for path in self._raw_paths:
                if path.startswith('/'):
                    return True
            return False
        return self.parser.isabs(self)

    def is_reserved(self):
        """Return True if the path contains one of the special names reserved
        by the system, if any."""
        import warnings
        msg = ("pathlib.PurePath.is_reserved() is deprecated and scheduled "
               "for removal in Python 3.15. Use os.path.isreserved() to "
               "detect reserved paths on Windows.")
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
        if self.parser is ntpath:
            return self.parser.isreserved(self)
        return False

    def as_uri(self):
        """Return the path as a URI."""
        if not self.is_absolute():
            raise ValueError("relative path can't be expressed as a file URI")

        drive = self.drive
        if len(drive) == 2 and drive[1] == ':':
            # It's a path on a local drive => 'file:///c:/a/b'
            prefix = 'file:///' + drive
            path = self.as_posix()[2:]
        elif drive:
            # It's a path on a network drive => 'file://host/share/a/b'
            prefix = 'file:'
            path = self.as_posix()
        else:
            # It's a posix path => 'file:///etc/hosts'
            prefix = 'file://'
            path = str(self)
        from urllib.parse import quote_from_bytes
        return prefix + quote_from_bytes(os.fsencode(path))

    def full_match(self, pattern, *, case_sensitive=None):
        """
        Return True if this path matches the given glob-style pattern. The
        pattern is matched against the entire path.
        """
        if not isinstance(pattern, PurePath):
            pattern = self.with_segments(pattern)
        if case_sensitive is None:
            case_sensitive = self.parser is posixpath

        # The string representation of an empty path is a single dot ('.'). Empty
        # paths shouldn't match wildcards, so we change it to the empty string.
        path = str(self) if self.parts else ''
        pattern = str(pattern) if pattern.parts else ''
        globber = _StringGlobber(self.parser.sep, case_sensitive, recursive=True)
        return globber.compile(pattern)(path) is not None

    def match(self, path_pattern, *, case_sensitive=None):
        """
        Return True if this path matches the given pattern. If the pattern is
        relative, matching is done from the right; otherwise, the entire path
        is matched. The recursive wildcard '**' is *not* supported by this
        method.
        """
        if not isinstance(path_pattern, PurePath):
            path_pattern = self.with_segments(path_pattern)
        if case_sensitive is None:
            case_sensitive = self.parser is posixpath
        path_parts = self.parts[::-1]
        pattern_parts = path_pattern.parts[::-1]
        if not pattern_parts:
            raise ValueError("empty pattern")
        if len(path_parts) < len(pattern_parts):
            return False
        if len(path_parts) > len(pattern_parts) and path_pattern.anchor:
            return False
        globber = _StringGlobber(self.parser.sep, case_sensitive)
        for path_part, pattern_part in zip(path_parts, pattern_parts):
            match = globber.compile(pattern_part)
            if match(path_part) is None:
                return False
        return True

# Subclassing os.PathLike makes isinstance() checks slower,
# which in turn makes Path construction slower. Register instead!
os.PathLike.register(PurePath)


class PurePosixPath(PurePath):
    """PurePath subclass for non-Windows systems.

    On a POSIX system, instantiating a PurePath should return this object.
    However, you can also instantiate it directly on any system.
    """
    parser = posixpath
    __slots__ = ()


class PureWindowsPath(PurePath):
    """PurePath subclass for Windows systems.

    On a Windows system, instantiating a PurePath should return this object.
    However, you can also instantiate it directly on any system.
    """
    parser = ntpath
    __slots__ = ()


class Path(WritablePath, ReadablePath, PurePath):
    """PurePath subclass that can make system calls.

    Path represents a filesystem path but unlike PurePath, also offers
    methods to do system calls on path objects. Depending on your system,
    instantiating a Path will return either a PosixPath or a WindowsPath
    object. You can also instantiate a PosixPath or WindowsPath directly,
    but cannot instantiate a WindowsPath on a POSIX system or vice versa.
    """
    __slots__ = ('_info',)

    def __new__(cls, *args, **kwargs):
        if cls is Path:
            cls = WindowsPath if os.name == 'nt' else PosixPath
        return object.__new__(cls)

    @property
    def info(self):
        """
        A PathInfo object that exposes the file type and other file attributes
        of this path.
        """
        try:
            return self._info
        except AttributeError:
            self._info = PathInfo(self)
            return self._info

    def stat(self, *, follow_symlinks=True):
        """
        Return the result of the stat() system call on this path, like
        os.stat() does.
        """
        return os.stat(self, follow_symlinks=follow_symlinks)

    def lstat(self):
        """
        Like stat(), except if the path points to a symlink, the symlink's
        status information is returned, rather than its target's.
        """
        return os.lstat(self)

    def exists(self, *, follow_symlinks=True):
        """
        Whether this path exists.

        This method normally follows symlinks; to check whether a symlink exists,
        add the argument follow_symlinks=False.
        """
        if follow_symlinks:
            return os.path.exists(self)
        return os.path.lexists(self)

    def is_dir(self, *, follow_symlinks=True):
        """
        Whether this path is a directory.
        """
        if follow_symlinks:
            return os.path.isdir(self)
        try:
            return S_ISDIR(self.stat(follow_symlinks=follow_symlinks).st_mode)
        except (OSError, ValueError):
            return False

    def is_file(self, *, follow_symlinks=True):
        """
        Whether this path is a regular file (also True for symlinks pointing
        to regular files).
        """
        if follow_symlinks:
            return os.path.isfile(self)
        try:
            return S_ISREG(self.stat(follow_symlinks=follow_symlinks).st_mode)
        except (OSError, ValueError):
            return False

    def is_mount(self):
        """
        Check if this path is a mount point
        """
        return os.path.ismount(self)

    def is_symlink(self):
        """
        Whether this path is a symbolic link.
        """
        return os.path.islink(self)

    def is_junction(self):
        """
        Whether this path is a junction.
        """
        return os.path.isjunction(self)

    def is_block_device(self):
        """
        Whether this path is a block device.
        """
        try:
            return S_ISBLK(self.stat().st_mode)
        except (OSError, ValueError):
            return False

    def is_char_device(self):
        """
        Whether this path is a character device.
        """
        try:
            return S_ISCHR(self.stat().st_mode)
        except (OSError, ValueError):
            return False

    def is_fifo(self):
        """
        Whether this path is a FIFO.
        """
        try:
            return S_ISFIFO(self.stat().st_mode)
        except (OSError, ValueError):
            return False

    def is_socket(self):
        """
        Whether this path is a socket.
        """
        try:
            return S_ISSOCK(self.stat().st_mode)
        except (OSError, ValueError):
            return False

    def samefile(self, other_path):
        """Return whether other_path is the same or not as this file
        (as returned by os.path.samefile()).
        """
        st = self.stat()
        try:
            other_st = other_path.stat()
        except AttributeError:
            other_st = self.with_segments(other_path).stat()
        return (st.st_ino == other_st.st_ino and
                st.st_dev == other_st.st_dev)

    def open(self, mode='r', buffering=-1, encoding=None,
             errors=None, newline=None):
        """
        Open the file pointed to by this path and return a file object, as
        the built-in open() function does.
        """
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
        return io.open(self, mode, buffering, encoding, errors, newline)

    def read_bytes(self):
        """
        Open the file in bytes mode, read it, and close the file.
        """
        with self.open(mode='rb', buffering=0) as f:
            return f.read()

    def read_text(self, encoding=None, errors=None, newline=None):
        """
        Open the file in text mode, read it, and close the file.
        """
        # Call io.text_encoding() here to ensure any warning is raised at an
        # appropriate stack level.
        encoding = io.text_encoding(encoding)
        with self.open(mode='r', encoding=encoding, errors=errors, newline=newline) as f:
            return f.read()

    def write_bytes(self, data):
        """
        Open the file in bytes mode, write to it, and close the file.
        """
        # type-check for the buffer interface before truncating the file
        view = memoryview(data)
        with self.open(mode='wb') as f:
            return f.write(view)

    def write_text(self, data, encoding=None, errors=None, newline=None):
        """
        Open the file in text mode, write to it, and close the file.
        """
        # Call io.text_encoding() here to ensure any warning is raised at an
        # appropriate stack level.
        encoding = io.text_encoding(encoding)
        if not isinstance(data, str):
            raise TypeError('data must be str, not %s' %
                            data.__class__.__name__)
        with self.open(mode='w', encoding=encoding, errors=errors, newline=newline) as f:
            return f.write(data)

    _remove_leading_dot = operator.itemgetter(slice(2, None))
    _remove_trailing_slash = operator.itemgetter(slice(-1))

    def _filter_trailing_slash(self, paths):
        sep = self.parser.sep
        anchor_len = len(self.anchor)
        for path_str in paths:
            if len(path_str) > anchor_len and path_str[-1] == sep:
                path_str = path_str[:-1]
            yield path_str

    def _from_dir_entry(self, dir_entry, path_str):
        path = self.with_segments(path_str)
        path._str = path_str
        path._info = DirEntryInfo(dir_entry)
        return path

    def iterdir(self):
        """Yield path objects of the directory contents.

        The children are yielded in arbitrary order, and the
        special entries '.' and '..' are not included.
        """
        root_dir = str(self)
        with os.scandir(root_dir) as scandir_it:
            entries = list(scandir_it)
        if root_dir == '.':
            return (self._from_dir_entry(e, e.name) for e in entries)
        else:
            return (self._from_dir_entry(e, e.path) for e in entries)

    def glob(self, pattern, *, case_sensitive=None, recurse_symlinks=False):
        """Iterate over this subtree and yield all existing files (of any
        kind, including directories) matching the given relative pattern.
        """
        sys.audit("pathlib.Path.glob", self, pattern)
        if case_sensitive is None:
            case_sensitive = self.parser is posixpath
            case_pedantic = False
        else:
            # The user has expressed a case sensitivity choice, but we don't
            # know the case sensitivity of the underlying filesystem, so we
            # must use scandir() for everything, including non-wildcard parts.
            case_pedantic = True
        parts = self._parse_pattern(pattern)
        recursive = True if recurse_symlinks else _no_recurse_symlinks
        globber = _StringGlobber(self.parser.sep, case_sensitive, case_pedantic, recursive)
        select = globber.selector(parts[::-1])
        root = str(self)
        paths = select(self.parser.join(root, ''))

        # Normalize results
        if root == '.':
            paths = map(self._remove_leading_dot, paths)
        if parts[-1] == '':
            paths = map(self._remove_trailing_slash, paths)
        elif parts[-1] == '**':
            paths = self._filter_trailing_slash(paths)
        paths = map(self._from_parsed_string, paths)
        return paths

    def rglob(self, pattern, *, case_sensitive=None, recurse_symlinks=False):
        """Recursively yield all existing files (of any kind, including
        directories) matching the given relative pattern, anywhere in
        this subtree.
        """
        sys.audit("pathlib.Path.rglob", self, pattern)
        pattern = self.parser.join('**', pattern)
        return self.glob(pattern, case_sensitive=case_sensitive, recurse_symlinks=recurse_symlinks)

    def walk(self, top_down=True, on_error=None, follow_symlinks=False):
        """Walk the directory tree from this directory, similar to os.walk()."""
        sys.audit("pathlib.Path.walk", self, on_error, follow_symlinks)
        root_dir = str(self)
        if not follow_symlinks:
            follow_symlinks = os._walk_symlinks_as_files
        results = os.walk(root_dir, top_down, on_error, follow_symlinks)
        for path_str, dirnames, filenames in results:
            if root_dir == '.':
                path_str = path_str[2:]
            yield self._from_parsed_string(path_str), dirnames, filenames

    def absolute(self):
        """Return an absolute version of this path
        No normalization or symlink resolution is performed.

        Use resolve() to resolve symlinks and remove '..' segments.
        """
        if self.is_absolute():
            return self
        if self.root:
            drive = os.path.splitroot(os.getcwd())[0]
            return self._from_parsed_parts(drive, self.root, self._tail)
        if self.drive:
            # There is a CWD on each drive-letter drive.
            cwd = os.path.abspath(self.drive)
        else:
            cwd = os.getcwd()
        if not self._tail:
            # Fast path for "empty" paths, e.g. Path("."), Path("") or Path().
            # We pass only one argument to with_segments() to avoid the cost
            # of joining, and we exploit the fact that getcwd() returns a
            # fully-normalized string by storing it in _str. This is used to
            # implement Path.cwd().
            return self._from_parsed_string(cwd)
        drive, root, rel = os.path.splitroot(cwd)
        if not rel:
            return self._from_parsed_parts(drive, root, self._tail)
        tail = rel.split(self.parser.sep)
        tail.extend(self._tail)
        return self._from_parsed_parts(drive, root, tail)

    @classmethod
    def cwd(cls):
        """Return a new path pointing to the current working directory."""
        cwd = os.getcwd()
        path = cls(cwd)
        path._str = cwd  # getcwd() returns a normalized path
        return path

    def resolve(self, strict=False):
        """
        Make the path absolute, resolving all symlinks on the way and also
        normalizing it.
        """

        return self.with_segments(os.path.realpath(self, strict=strict))

    if pwd:
        def owner(self, *, follow_symlinks=True):
            """
            Return the login name of the file owner.
            """
            uid = self.stat(follow_symlinks=follow_symlinks).st_uid
            return pwd.getpwuid(uid).pw_name
    else:
        def owner(self, *, follow_symlinks=True):
            """
            Return the login name of the file owner.
            """
            f = f"{type(self).__name__}.owner()"
            raise UnsupportedOperation(f"{f} is unsupported on this system")

    if grp:
        def group(self, *, follow_symlinks=True):
            """
            Return the group name of the file gid.
            """
            gid = self.stat(follow_symlinks=follow_symlinks).st_gid
            return grp.getgrgid(gid).gr_name
    else:
        def group(self, *, follow_symlinks=True):
            """
            Return the group name of the file gid.
            """
            f = f"{type(self).__name__}.group()"
            raise UnsupportedOperation(f"{f} is unsupported on this system")

    if hasattr(os, "readlink"):
        def readlink(self):
            """
            Return the path to which the symbolic link points.
            """
            return self.with_segments(os.readlink(self))
    else:
        def readlink(self):
            """
            Return the path to which the symbolic link points.
            """
            f = f"{type(self).__name__}.readlink()"
            raise UnsupportedOperation(f"{f} is unsupported on this system")

    def touch(self, mode=0o666, exist_ok=True):
        """
        Create this file with the given access mode, if it doesn't exist.
        """

        if exist_ok:
            # First try to bump modification time
            # Implementation note: GNU touch uses the UTIME_NOW option of
            # the utimensat() / futimens() functions.
            try:
                os.utime(self, None)
            except OSError:
                # Avoid exception chaining
                pass
            else:
                return
        flags = os.O_CREAT | os.O_WRONLY
        if not exist_ok:
            flags |= os.O_EXCL
        fd = os.open(self, flags, mode)
        os.close(fd)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        """
        Create a new directory at this given path.
        """
        try:
            os.mkdir(self, mode)
        except FileNotFoundError:
            if not parents or self.parent == self:
                raise
            self.parent.mkdir(parents=True, exist_ok=True)
            self.mkdir(mode, parents=False, exist_ok=exist_ok)
        except OSError:
            # Cannot rely on checking for EEXIST, since the operating system
            # could give priority to other errors like EACCES or EROFS
            if not exist_ok or not self.is_dir():
                raise

    def chmod(self, mode, *, follow_symlinks=True):
        """
        Change the permissions of the path, like os.chmod().
        """
        os.chmod(self, mode, follow_symlinks=follow_symlinks)

    def lchmod(self, mode):
        """
        Like chmod(), except if the path points to a symlink, the symlink's
        permissions are changed, rather than its target's.
        """
        self.chmod(mode, follow_symlinks=False)

    def unlink(self, missing_ok=False):
        """
        Remove this file or link.
        If the path is a directory, use rmdir() instead.
        """
        try:
            os.unlink(self)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rmdir(self):
        """
        Remove this directory.  The directory must be empty.
        """
        os.rmdir(self)

    def _delete(self):
        """
        Delete this file or directory (including all sub-directories).
        """
        if self.is_symlink() or self.is_junction():
            self.unlink()
        elif self.is_dir():
            # Lazy import to improve module import time
            import shutil
            shutil.rmtree(self)
        else:
            self.unlink()

    def rename(self, target):
        """
        Rename this path to the target path.

        The target path may be absolute or relative. Relative paths are
        interpreted relative to the current working directory, *not* the
        directory of the Path object.

        Returns the new Path instance pointing to the target path.
        """
        os.rename(self, target)
        return self.with_segments(target)

    def replace(self, target):
        """
        Rename this path to the target path, overwriting if that path exists.

        The target path may be absolute or relative. Relative paths are
        interpreted relative to the current working directory, *not* the
        directory of the Path object.

        Returns the new Path instance pointing to the target path.
        """
        os.replace(self, target)
        return self.with_segments(target)

    _copy_reader = property(_LocalCopyReader)
    _copy_writer = property(_LocalCopyWriter)

    def move(self, target):
        """
        Recursively move this file or directory tree to the given destination.
        """
        # Use os.replace() if the target is os.PathLike and on the same FS.
        try:
            target_str = os.fspath(target)
        except TypeError:
            pass
        else:
            if not hasattr(target, '_copy_writer'):
                target = self.with_segments(target_str)
            target._copy_writer._ensure_different_file(self)
            try:
                os.replace(self, target_str)
                return target
            except OSError as err:
                if err.errno != EXDEV:
                    raise
        # Fall back to copy+delete.
        target = self.copy(target, follow_symlinks=False, preserve_metadata=True)
        self._delete()
        return target

    def move_into(self, target_dir):
        """
        Move this file or directory tree into the given existing directory.
        """
        name = self.name
        if not name:
            raise ValueError(f"{self!r} has an empty name")
        elif hasattr(target_dir, '_copy_writer'):
            target = target_dir / name
        else:
            target = self.with_segments(target_dir, name)
        return self.move(target)

    if hasattr(os, "symlink"):
        def symlink_to(self, target, target_is_directory=False):
            """
            Make this path a symlink pointing to the target path.
            Note the order of arguments (link, target) is the reverse of os.symlink.
            """
            os.symlink(target, self, target_is_directory)
    else:
        def symlink_to(self, target, target_is_directory=False):
            """
            Make this path a symlink pointing to the target path.
            Note the order of arguments (link, target) is the reverse of os.symlink.
            """
            f = f"{type(self).__name__}.symlink_to()"
            raise UnsupportedOperation(f"{f} is unsupported on this system")

    if hasattr(os, "link"):
        def hardlink_to(self, target):
            """
            Make this path a hard link pointing to the same file as *target*.

            Note the order of arguments (self, target) is the reverse of os.link's.
            """
            os.link(target, self)
    else:
        def hardlink_to(self, target):
            """
            Make this path a hard link pointing to the same file as *target*.

            Note the order of arguments (self, target) is the reverse of os.link's.
            """
            f = f"{type(self).__name__}.hardlink_to()"
            raise UnsupportedOperation(f"{f} is unsupported on this system")

    def expanduser(self):
        """ Return a new path with expanded ~ and ~user constructs
        (as returned by os.path.expanduser)
        """
        if (not (self.drive or self.root) and
            self._tail and self._tail[0][:1] == '~'):
            homedir = os.path.expanduser(self._tail[0])
            if homedir[:1] == "~":
                raise RuntimeError("Could not determine home directory.")
            drv, root, tail = self._parse_path(homedir)
            return self._from_parsed_parts(drv, root, tail + self._tail[1:])

        return self

    @classmethod
    def home(cls):
        """Return a new path pointing to expanduser('~').
        """
        homedir = os.path.expanduser("~")
        if homedir == "~":
            raise RuntimeError("Could not determine home directory.")
        return cls(homedir)

    @classmethod
    def from_uri(cls, uri):
        """Return a new path from the given 'file' URI."""
        if not uri.startswith('file:'):
            raise ValueError(f"URI does not start with 'file:': {uri!r}")
        path = uri[5:]
        if path[:3] == '///':
            # Remove empty authority
            path = path[2:]
        elif path[:12] == '//localhost/':
            # Remove 'localhost' authority
            path = path[11:]
        if path[:3] == '///' or (path[:1] == '/' and path[2:3] in ':|'):
            # Remove slash before DOS device/UNC path
            path = path[1:]
        if path[1:2] == '|':
            # Replace bar with colon in DOS drive
            path = path[:1] + ':' + path[2:]
        from urllib.parse import unquote_to_bytes
        path = cls(os.fsdecode(unquote_to_bytes(path)))
        if not path.is_absolute():
            raise ValueError(f"URI is not absolute: {uri!r}")
        return path


class PosixPath(Path, PurePosixPath):
    """Path subclass for non-Windows systems.

    On a POSIX system, instantiating a Path should return this object.
    """
    __slots__ = ()

    if os.name == 'nt':
        def __new__(cls, *args, **kwargs):
            raise UnsupportedOperation(
                f"cannot instantiate {cls.__name__!r} on your system")

class WindowsPath(Path, PureWindowsPath):
    """Path subclass for Windows systems.

    On a Windows system, instantiating a Path should return this object.
    """
    __slots__ = ()

    if os.name != 'nt':
        def __new__(cls, *args, **kwargs):
            raise UnsupportedOperation(
                f"cannot instantiate {cls.__name__!r} on your system")
