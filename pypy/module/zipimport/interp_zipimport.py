import os
import stat

from rpython.annotator.model import s_Str0
from rpython.rlib.objectmodel import enforceargs
from rpython.rlib.unroll import unrolling_iterable
from rpython.rlib.rzipfile import RZipFile, BadZipfile
from rpython.rlib.rzlib import RZlibError
from rpython.rlib.rstring import assert_str0
from rpython.rlib.signature import signature, finishsigs
from rpython.rlib import types

from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.gateway import interp2app, unwrap_spec
from pypy.interpreter.typedef import TypeDef, GetSetProperty
from pypy.interpreter.module import Module
from pypy.module.imp import importing
from pypy.module.zlib.interp_zlib import zlib_error

ZIPSEP = '/'
# note that zipfiles always use slash, but for OSes with other
# separators, we need to pretend that we had the os.sep.

ENUMERATE_EXTS = unrolling_iterable(
    [(True, True, ZIPSEP + '__init__.pyc'),
     (False, True, ZIPSEP + '__init__.py'),
     (True, False, '.pyc'),
     (False, False, '.py')])

class Cache:
    def __init__(self, space):
        self.w_error = space.new_exception_class("zipimport.ZipImportError",
                                                 space.w_ImportError)

def get_error(space):
    return space.fromcache(Cache).w_error

class W_ZipCache(W_Root):
    def __init__(self):
        self.cache = {}

    def get(self, name):
        return self.cache[name]

    def set(self, name, w_importer):
        self.cache[name] = w_importer

    # -------------- dict-like interface -----------------
    # I don't care about speed of those, they're obscure anyway
    # THIS IS A TERRIBLE HACK TO BE CPYTHON COMPATIBLE

    def getitem(self, space, w_name):
        return self._getitem(space, space.text_w(w_name))

    def _getitem(self, space, name):
        try:
            w_zipimporter = self.cache[name]
        except KeyError:
            raise OperationError(space.w_KeyError, space.newfilename(name))
        assert isinstance(w_zipimporter, W_ZipImporter)
        w_fs = space.newfilename
        w_d = space.newdict()
        for key, info in w_zipimporter.zip_file.NameToInfo.iteritems():
            if ZIPSEP != os.path.sep:
                key = key.replace(ZIPSEP, os.path.sep)
            space.setitem(w_d, w_fs(key), space.newtuple([
                w_fs(info.filename), space.newint(info.compress_type), space.newint(info.compress_size),
                space.newint(info.file_size), space.newint(info.file_offset), space.newint(info.dostime),
                space.newint(info.dosdate), space.newint(info.CRC)]))
        return w_d

    def keys(self, space):
        return space.newlist([space.newfilename(s)
                              for s in self.cache.keys()])

    def values(self, space):
        keys = self.cache.keys()
        values_w = [self._getitem(space, key) for key in keys]
        return space.newlist(values_w)

    def items(self, space):
        w_fs = space.newfilename
        items_w = [space.newtuple([w_fs(key), self._getitem(space, key)])
                   for key in self.cache.keys()]
        return space.newlist(items_w)

    def iteratekeys(self, space):
        return space.iter(self.keys(space))

    def iteratevalues(self, space):
        return space.iter(self.values(space))

    def iteritems(self, space):
        return space.iter(self.items(space))

    @unwrap_spec(name='text')
    def contains(self, space, name):
        return space.newbool(name in self.cache)

    def clear(self, space):
        self.cache = {}

    @unwrap_spec(name='text')
    def delitem(self, space, name):
        del self.cache[name]

W_ZipCache.typedef = TypeDef(
    'zip_dict',
    __getitem__ = interp2app(W_ZipCache.getitem),
    __contains__ = interp2app(W_ZipCache.contains),
    __iter__ = interp2app(W_ZipCache.iteratekeys),
    items = interp2app(W_ZipCache.items),
    iteritems = interp2app(W_ZipCache.iteritems),
    keys = interp2app(W_ZipCache.keys),
    iterkeys = interp2app(W_ZipCache.iteratekeys),
    values = interp2app(W_ZipCache.values),
    itervalues = interp2app(W_ZipCache.iteratevalues),
    clear = interp2app(W_ZipCache.clear),
    __delitem__ = interp2app(W_ZipCache.delitem),
)

zip_cache = W_ZipCache()

@finishsigs
class W_ZipImporter(W_Root):
    def __init__(self, space, name, filename, zip_file, prefix):
        self.space = space
        self.name = name
        self.filename = filename
        self.zip_file = zip_file
        self.prefix = prefix

    def getprefix(self, space):
        if ZIPSEP == os.path.sep:
            return space.newfilename(self.prefix)
        return space.newfilename(self.prefix.replace(ZIPSEP, os.path.sep))

    def _find_relative_path(self, filename):
        if filename.startswith(self.filename):
            filename = filename[len(self.filename):]
        if filename.startswith(os.path.sep) or filename.startswith(ZIPSEP):
            filename = filename[1:]
        if ZIPSEP != os.path.sep:
            filename = filename.replace(os.path.sep, ZIPSEP)
        return filename

    @signature(types.self(), types.str0(), returns=types.str0())
    def corr_zname(self, fname):
        if ZIPSEP != os.path.sep:
            return fname.replace(ZIPSEP, os.path.sep)
        else:
            return fname

    @enforceargs(filename=s_Str0, typecheck=False)
    def import_py_file(self, space, modname, filename, buf, pkgpath):
        w_mod = Module(space, space.newtext(modname))
        real_name = self.filename + os.path.sep + self.corr_zname(filename)
        space.setattr(w_mod, space.newtext('__loader__'), self)
        importing._prepare_module(space, w_mod, real_name, pkgpath)
        co_filename = self.make_co_filename(filename)
        code_w = importing.parse_source_module(space, co_filename, buf)
        importing.exec_code_module(space, w_mod, code_w, co_filename, None)
        return w_mod

    def _parse_mtime(self, space, filename):
        try:
            info = self.zip_file.NameToInfo[filename]
            t = info.date_time
        except KeyError:
            return 0
        else:
            w_mktime = space.getattr(space.getbuiltinmodule('time'),
                                     space.newtext('mktime'))
            # XXX this is incredible fishing around module limitations
            #     in order to compare timestamps of .py and .pyc files
            # we need time.mktime support on rpython level
            w = space.newint
            all = [w(t[0]), w(t[1]), w(t[2]), w(t[3]), w(t[4]),
                   w(t[5]), w(0), w(1), w(-1)]
            mtime = int(space.float_w(space.call_function(
                w_mktime, space.newtuple(all))))
            return mtime

    def check_newer_pyfile(self, space, filename, timestamp):
        # check if the timestamp stored in the .pyc is matching
        # the actual timestamp of the .py file, if any
        mtime = self._parse_mtime(space, filename)
        if mtime == 0:
            return False
        # Lenient date/time comparison function. The precision of the mtime
        # in the archive is lower than the mtime stored in a .pyc: we
        # must allow a difference of at most one second.
        d = mtime - timestamp
        if d < 0:
            d = -d
        return d > 1    # more than one second => different

    def can_use_pyc(self, space, filename, magic, timestamp):
        if magic != importing.get_pyc_magic(space):
            return False
        if self.check_newer_pyfile(space, filename[:-1], timestamp):
            return False
        return True

    @enforceargs(filename=s_Str0, typecheck=False)
    def import_pyc_file(self, space, modname, filename, buf, pkgpath):
        magic = importing._get_long(buf[:4])
        timestamp = importing._get_long(buf[4:8])
        if not self.can_use_pyc(space, filename, magic, timestamp):
            return None
        # zipimport ignores the size field
        buf = buf[12:]  # XXX ugly copy, should use sequential read instead
        w_mod = Module(space, space.newtext(modname))
        real_name = self.filename + os.path.sep + self.corr_zname(filename)
        space.setattr(w_mod, space.newtext('__loader__'), self)
        importing._prepare_module(space, w_mod, real_name, pkgpath)
        result = importing.load_compiled_module(
            space, space.newtext(modname),
            w_mod, real_name, magic, timestamp, buf)
        return result

    def have_modulefile(self, space, filename):
        if ZIPSEP != os.path.sep:
            filename = filename.replace(os.path.sep, ZIPSEP)
        try:
            self.zip_file.NameToInfo[filename]
            return True
        except KeyError:
            return False

    @unwrap_spec(fullname='text')
    def find_module(self, space, fullname, w_path=None):
        filename = self.make_filename(fullname)
        for _, _, ext in ENUMERATE_EXTS:
            if self.have_modulefile(space, filename + ext):
                return self

    def make_filename(self, fullname):
        startpos = fullname.rfind('.') + 1  # 0 when not found
        assert startpos >= 0
        subname = fullname[startpos:]
        if ZIPSEP == os.path.sep:
            return self.prefix + subname.replace('.', '/')
        else:
            return (self.prefix.replace(os.path.sep, ZIPSEP) +
                    subname.replace('.', '/'))

    def make_co_filename(self, filename):
        """
        Return the filename to be used for compiling the module, i.e. what
        gets in code_object.co_filename. Something like
        'myfile.zip/mymodule.py'
        """
        if ZIPSEP != os.path.sep:
            filename = filename.replace(ZIPSEP, os.path.sep)
        return self.filename + os.path.sep + filename

    def load_module(self, space, w_fullname):
        fullname = space.text_w(w_fullname)
        filename = self.make_filename(fullname)
        for compiled, is_package, ext in ENUMERATE_EXTS:
            if '\x00' in filename:
                # Special case to make the annotator happy:
                # filenames inside ZIPs shouldn't contain NULs so no module can
                # possibly be found in this case
                break
            filename = assert_str0(filename)
            fname = filename + ext
            try:
                buf = self.zip_file.read(fname)
            except (KeyError, OSError, BadZipfile):
                pass
            except RZlibError as e:
                # in this case, CPython raises the direct exception coming
                # from the zlib module: let's do the same
                raise zlib_error(space, e.msg)
            else:
                if is_package:
                    pkgpath = (self.filename + os.path.sep +
                               self.corr_zname(filename))
                else:
                    pkgpath = None
                try:
                    if compiled:
                        w_result = self.import_pyc_file(space, fullname, fname,
                                                        buf, pkgpath)
                        if w_result is None:
                            continue
                    else:
                        w_result = self.import_py_file(space, fullname, fname,
                                                   buf, pkgpath)
                    if space.sys.get_flag('verbose') >= 1:
                        w_stderr = space.sys.get('stderr')
                        message = "import %s # loaded from Zip %s%s%s\n" % (
                                fullname, self.filename, os.path.sep, fname)
                        space.call_method(w_stderr, "write",
                                          space.newtext(message))
                    return w_result
                except:
                    w_mods = space.sys.get('modules')
                    space.call_method(w_mods, 'pop', w_fullname, space.w_None)
                    raise
        raise oefmt(get_error(space), "can't find module %R", w_fullname)

    @unwrap_spec(filename='text')
    def get_data(self, space, filename):
        filename = self._find_relative_path(filename)
        try:
            data = self.zip_file.read(filename)
            return space.newbytes(data)
        except (KeyError, OSError, BadZipfile):
            raise oefmt(space.w_IOError, "Error reading file")
        except RZlibError as e:
            # in this case, CPython raises the direct exception coming
            # from the zlib module: let's do the same
            raise zlib_error(space, e.msg)

    def get_code(self, space, w_fullname):
        fullname = space.text_w(w_fullname)
        filename = self.make_filename(fullname)
        for compiled, _, ext in ENUMERATE_EXTS:
            if '\x00' in filename:
                # Special case to make the annotator happy:
                # filenames inside ZIPs shouldn't contain NULs so no module can
                # possibly be found in this case
                break
            filename = assert_str0(filename)
            if self.have_modulefile(space, filename + ext):
                w_source = self.get_data(space, filename + ext)
                source = space.bytes_w(w_source)
                if compiled:
                    magic = importing._get_long(source[:4])
                    timestamp = importing._get_long(source[4:8])
                    if not self.can_use_pyc(space, filename + ext,
                                            magic, timestamp):
                        continue
                    # zipimport ignores the size field
                    w_code = importing.read_compiled_module(
                        space, filename + ext, source[12:])
                else:
                    co_filename = self.make_co_filename(filename+ext)
                    w_code = importing.parse_source_module(
                        space, co_filename, source)
                return w_code
        raise oefmt(get_error(space),
                    "Cannot find source or code for %R in %R",
                    w_fullname, space.newfilename(self.name))

    @unwrap_spec(fullname='text')
    def get_source(self, space, fullname):
        filename = self.make_filename(fullname)
        found = False
        for compiled, _, ext in ENUMERATE_EXTS:
            if '\x00' in filename:
                # Special case to make the annotator happy:
                # filenames inside ZIPs shouldn't contain NULs so no module can
                # possibly be found in this case
                break
            filename = assert_str0(filename)
            fname = filename + ext
            if self.have_modulefile(space, fname):
                if not compiled:
                    w_data = self.get_data(space, fname)
                    # XXX CPython does not handle the coding cookie either.
                    return space.call_method(w_data, "decode",
                                             space.newtext("utf-8"))
                else:
                    found = True
        if found:
            # We have the module, but no source.
            return space.w_None
        raise oefmt(get_error(space),
                    "Cannot find source for %R in %R",
                    space.newfilename(filename),
                    space.newfilename(self.name))

    def get_filename(self, space, w_fullname):
        fullname = space.text_w(w_fullname)
        filename = self.make_filename(fullname)
        for _, is_package, ext in ENUMERATE_EXTS:
            if '\x00' in filename:
                # Special case to make the annotator happy:
                # filenames inside ZIPs shouldn't contain NULs so no module can
                # possibly be found in this case
                break
            filename = assert_str0(filename)
            if self.have_modulefile(space, filename + ext):
                return space.newfilename(self.filename + os.path.sep +
                                            self.corr_zname(filename + ext))
        raise oefmt(get_error(space),
                    "Cannot find module %R in %R",
                    space.newfilename(filename),
                    space.newfilename(self.name))

    def is_package(self, space, w_fullname):
        fullname = space.text_w(w_fullname)
        filename = self.make_filename(fullname)
        for _, is_package, ext in ENUMERATE_EXTS:
            if '\x00' in filename:
                # Special case to make the annotator happy:
                # filenames inside ZIPs shouldn't contain NULs so no module can
                # possibly be found in this case
                break
            filename = assert_str0(filename)
            if self.have_modulefile(space, filename + ext):
                return space.newbool(is_package)
        raise oefmt(get_error(space),
                    "Cannot find module %R in %R",
                    space.newfilename(filename),
                    space.newfilename(self.name))

    def getarchive(self, space):
        space = self.space
        return space.newfilename(self.filename)

    def _find_loader(self, space, fullname):
        if '\x00' in fullname:
            # Special case to make the annotator happy:
            # filenames inside ZIPs shouldn't contain NULs so no module can
            # possibly be found in this case
            return False, None
        fullname = assert_str0(fullname)
        filename = self.make_filename(fullname)
        filename = assert_str0(filename)
        for _, _, ext in ENUMERATE_EXTS:
            if self.have_modulefile(space, filename + ext):
                return True, None
        # See if this is a directory (part of a namespace pkg)
        dirpath = self.prefix + fullname.split(".")[-1]
        if self.have_modulefile(space, dirpath + ZIPSEP):
            return True, self.filename + os.path.sep + self.corr_zname(dirpath)
        return False, None

    @unwrap_spec(fullname='text')
    def find_loader(self, space, fullname, w_path=None):
        found, ns_portion = self._find_loader(space, fullname)
        if not found:
            result = [space.w_None, space.newlist([])]
        elif not ns_portion:
            result = [self, space.newlist([])]
        else:
            result = [space.w_None,
                      space.newlist([space.newfilename(ns_portion)])]
        return space.newtuple(result)

def descr_new_zipimporter(space, w_type, w_name):
    name = space.fsencode_w(w_name)
    ok = False
    parts_ends = [i for i in range(0, len(name))
            if name[i] == os.path.sep or name[i] == ZIPSEP]
    parts_ends.append(len(name))
    filename = ""  # make annotator happy
    for i in parts_ends:
        filename = name[:i]
        if not filename:
            filename = os.path.sep
        try:
            s = os.stat(filename)
        except OSError:
            raise oefmt(get_error(space), "Cannot find name %R", w_name)
        if not stat.S_ISDIR(s.st_mode):
            ok = True
            break
    if not ok:
        raise oefmt(get_error(space), "Did not find %R to be a valid zippath",
                    w_name)
    try:
        w_result = zip_cache.get(filename)
        if w_result is None:
            raise oefmt(get_error(space),
                        "Cannot import %R from zipfile, recursion detected or"
                        "already tried and failed", w_name)
    except KeyError:
        zip_cache.cache[filename] = None
    try:
        zip_file = RZipFile(filename, 'r')
    except (BadZipfile, OSError):
        raise oefmt(get_error(space), "%R seems not to be a zipfile",
                    space.newfilename(filename))
    except RZlibError as e:
        # in this case, CPython raises the direct exception coming
        # from the zlib module: let's do the same
        raise zlib_error(space, e.msg)

    prefix = name[len(filename):]
    if prefix.startswith(os.path.sep) or prefix.startswith(ZIPSEP):
        prefix = prefix[1:]
    if prefix and not prefix.endswith(ZIPSEP) and not prefix.endswith(os.path.sep):
        prefix += ZIPSEP
    w_result = W_ZipImporter(space, name, filename, zip_file, prefix)
    zip_cache.set(filename, w_result)
    return w_result

W_ZipImporter.typedef = TypeDef(
    'zipimporter',
    __new__     = interp2app(descr_new_zipimporter),
    find_module = interp2app(W_ZipImporter.find_module),
    get_data    = interp2app(W_ZipImporter.get_data),
    get_code    = interp2app(W_ZipImporter.get_code),
    get_source  = interp2app(W_ZipImporter.get_source),
    get_filename = interp2app(W_ZipImporter.get_filename),
    is_package  = interp2app(W_ZipImporter.is_package),
    load_module = interp2app(W_ZipImporter.load_module),
    find_loader = interp2app(W_ZipImporter.find_loader),
    archive     = GetSetProperty(W_ZipImporter.getarchive),
    prefix      = GetSetProperty(W_ZipImporter.getprefix),
)
