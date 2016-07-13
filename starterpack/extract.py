"""Unpack downloaded files to the appropriate place.

This module is about as generic as it can usefully be, pushing the special
cases back into build.py
"""

import concurrent.futures
from distutils.dir_util import copy_tree
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

from . import component
from . import paths

DFHACK_VER = None


def _copyfile(src, dest):
    """Copy the source file path or object to the dest path, creating dirs."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if isinstance(src, str):
        shutil.copy2(src, dest)
    else:
        with open(dest, 'wb') as out:
            shutil.copyfileobj(src, out)


def unzip_to(filename, target_dir=None, path_pairs=None):
    """Extract the contents of the given archive to the target directory.

    In 'target_dir' mode, extracts the least-nested contents to the target
    directory.  This makes a zip-of-one-dir equivalent to zip-of-several-files.

    In 'path_pairs' mode, the argument should be a sequence of paths.
    The file at the first path within the zip is written at the second path.
    """
    assert bool(target_dir) != bool(path_pairs), 'Choose one unzip mode!'
    out = target_dir or os.path.commonpath([p[1] for p in path_pairs])
    print('{:20}  ->  {}'.format(os.path.basename(filename)[:20], out))

    if not zipfile.is_zipfile(filename) or filename.endswith('.exe'):
        return nonzip_extract(filename, target_dir, path_pairs)
    # More complex, but faster for zips to do it this way
    with zipfile.ZipFile(filename) as zf:
        files = dict(a for a in zip(zf.namelist(), zf.infolist())
                     if not a[0].endswith('/'))
        if path_pairs is not None:
            for inpath, outpath in path_pairs:
                if inpath in files:
                    _copyfile(zf.open(files[inpath]), outpath)
                else:
                    print('WARNING:  {} not in {}'.format(
                        inpath, os.path.basename(filename)))
        else:
            prefix = os.path.commonpath(list(files)) if len(files) > 1 else ''
            for name in files:
                out = os.path.join(target_dir, os.path.relpath(name, prefix))
                _copyfile(zf.open(files[name]), out)


def nonzip_extract(filename, target_dir=None, path_pairs=None):
    """An alternative to `unzip_to`, for non-zip archives.

    Extract to tempdir, copy files to destination/path_pairs, remove tempdir.

    Involves a lot of shelling out, as Python's `tarfile` cannot open
    the .tar.bz2 archived DF releases (complicated header issue).
    OSX disk images (.dmg) are also unsupported by Python.
    """
    if filename.endswith('.exe') and paths.HOST_OS == 'win':
        return _copyfile(
            filename, os.path.join(target_dir, os.path.basename(filename)))

    with tempfile.TemporaryDirectory() as tmpdir:
        if not unpack_anything(filename, tmpdir):
            return
        # Copy from tempdir to destination
        if target_dir:
            files = [os.path.join(root, f)
                     for root, _, files in os.walk(tmpdir) for f in files]
            prefix = os.path.commonpath(files) if len(files) > 1 else ''
            copy_tree(os.path.join(tmpdir, prefix), target_dir)
        else:
            for inpath, outpath in path_pairs:
                if os.path.isfile(os.path.join(tmpdir, inpath)):
                    _copyfile(os.path.join(tmpdir, inpath), outpath)
                else:
                    print('WARNING:  {} not in {}'.format(inpath, filename))


def unpack_anything(filename, tmpdir):
    """Extract practically any archive format from src file to dest dir."""
    if filename.endswith('.dmg') and paths.HOST_OS == 'osx':
        # TODO:  support .dmg extraction via shell on OSX
        raise NotImplementedError(
            'TODO: mount .dmg, copy contents to tmpdir, unmount')
    elif zipfile.is_zipfile(filename):
        # Uses fast version above; handled here for completeness
        zipfile.ZipFile(filename).extractall(tmpdir)
    elif any(filename.endswith('.tar.' + ext) for ext in ('bz2', 'xz', 'gz'))\
            or tarfile.is_tarfile(filename):
        try:
            tarfile.TarFile(filename).extractall(tmpdir)
        except tarfile.ReadError:
            try:
                subprocess.run(['tar', '-xf', filename, '-C', tmpdir],
                               check=True)
            except Exception:
                print('ERROR: could not extract ' + filename +
                      ' by tarfile lib or `tar` in shell')
                return False
    elif filename.endswith('.rar'):
        try:
            import rarfile
        except ImportError:
            print('ERROR: .rar not supported; `pip install rarfile` and retry')
            return False
        rarfile.RarFile(filename).extractall(tmpdir)
    else:
        print('Error: skipping unsupported archive format ' + filename)
        return False
    return True


def extract_df():
    """Extract Dwarf Fortress, and DFHack if available and compatible."""
    # A DF dir for the main install, for baselines, and for ASCII graphics
    with concurrent.futures.ProcessPoolExecutor() as ex:
        for p in (paths.df(), paths.curr_baseline(), paths.graphics('ASCII')):
            ex.submit(unzip_to, component.ALL['Dwarf Fortress'].path, p)

    hack = component.ALL.get('DFHack')
    if not hack:
        print('WARNING:  DFHack not in config, will not be installed.')
    elif paths.df_ver() not in hack.version:
        print('Incompatible DF, DFHack versions!  Aborting DFHack install...')
    else:
        unzip_to(hack.path, paths.df())
        return hack.version.replace('v', '')


def extract_files_utilities_and_graphics():
    """Extract the miscelaneous files in components.yml"""
    ex = concurrent.futures.ProcessPoolExecutor(8)
    for comp in component.ALL.values():
        if comp.name in ('Dwarf Fortress', 'DFHack'):
            continue
        if comp.needs_dfhack and DFHACK_VER is None:
            print(comp.name, 'not installed - requires DFHack')
            continue
        if comp.extract_to == '':
            ex.submit(unzip_to, comp.path, paths.lnp(comp.category, comp.name))
        else:
            if ':' not in comp.extract_to:
                # first part of extract_to is paths method, remainder is args
                dest, *details = comp.extract_to.split('/')
                ex.submit(unzip_to, comp.path, getattr(paths, dest)(*details))
            else:
                # using the path_pairs option; extract pairs from string
                pairs = []
                for pair in comp.extract_to.strip().split('\n'):
                    src, to = pair.split(':')
                    dest, *details = to.split('/')
                    # Note: can add format variables here as needed
                    src = src.format(DFHACK_VER=DFHACK_VER)
                    pairs.append([src, getattr(paths, dest)(*details)])
                ex.submit(unzip_to, comp.path, path_pairs=pairs)
    ex.shutdown()


def add_lnp_dirs():
    """Install the LNP subdirs that I can't create automatically."""
    # Should use https://github.com/Lazy-Newb-Pack/LNP-shared-core someday...
    for d in ('colors', 'embarks', 'extras', 'keybinds', 'tilesets'):
        shutil.copytree(paths.base(d), paths.lnp(d))


def main():
    """Extract all components, in the required order."""
    print('\nExtracting components...')
    if os.path.isdir('build'):
        shutil.rmtree('build')
    global DFHACK_VER  # pylint:disable=global-statement
    DFHACK_VER = extract_df()
    extract_files_utilities_and_graphics()
    add_lnp_dirs()
