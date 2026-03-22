"""
Creates a minimal deepspeed stub in the active venv so resemble-enhance can
be imported for inference on Windows (where the real deepspeed cannot be built).

resemble-enhance imports deepspeed at module level, but only *training* code
actually calls it. The inference functions (denoise/enhance) work fine with
this stub, which absorbs any attribute or call via PEP 562 __getattr__.
"""
import os
import shutil
import sysconfig
import sys

STUB_CODE = '''\
# deepspeed stub -- satisfies resemble-enhance imports on Windows
# Uses sys.meta_path with find_spec (Python 3.4+ API) to stub all deepspeed.* imports.
# Only training code calls deepspeed; inference (denoise/enhance) is safe with stubs.

import sys
import types as _types
import importlib.machinery as _machinery


class _Stub:
    # __mro_entries__ must return a tuple — returning () removes this from MRO
    def __mro_entries__(self, bases): return ()
    def __call__(self, *a, **kw): return _Stub()
    def __getattr__(self, n): return _Stub()
    def __iter__(self): return iter([_Stub()] * 4)
    def __bool__(self): return False
    def __repr__(self): return "<deepspeed stub>"


class _SubmoduleLoader:
    @staticmethod
    def create_module(spec): return None

    @staticmethod
    def exec_module(mod):
        # __file__ must be set so inspect.getfile() doesn't raise "is a built-in module"
        mod.__file__ = __file__
        mod.__getattr__ = lambda name: _Stub()


class _SubmoduleImporter:
    _loader = _SubmoduleLoader()

    def find_spec(self, fullname, path, target=None):
        if fullname.startswith("deepspeed."):
            spec = _machinery.ModuleSpec(
                name=fullname,
                loader=self._loader,
                is_package=True,
            )
            spec.submodule_search_locations = []
            return spec
        return None


# Install once
if not any(isinstance(f, _SubmoduleImporter) for f in sys.meta_path):
    sys.meta_path.append(_SubmoduleImporter())


# Module-level __getattr__ (PEP 562): handles "from deepspeed import X"
def __getattr__(name):
    return _Stub()


def initialize(*args, **kwargs):
    return _Stub(), _Stub(), _Stub(), _Stub()


DeepSpeedConfig = _Stub
DeepSpeedEngine = _Stub
PipelineEngine  = _Stub
zero = _Stub()
ops  = _Stub()
comm = _Stub()
accelerator = _Stub()
'''


def _candidate_dirs():
    """Return all plausible site-packages / venv-root directories to try."""
    dirs = []
    # 1. sysconfig purelib (most reliable cross-platform)
    purelib = sysconfig.get_path('purelib')
    if purelib:
        dirs.append(purelib)
    # 2. site.getsitepackages() (may include venv root on some Windows configs)
    try:
        import site
        dirs.extend(site.getsitepackages())
    except AttributeError:
        pass
    # 3. sys.prefix / Lib / site-packages (Windows fallback)
    dirs.append(os.path.join(sys.prefix, 'Lib', 'site-packages'))
    # 4. sys.prefix itself (venv root -- seen on some Windows setups)
    dirs.append(sys.prefix)
    # deduplicate while preserving order
    seen = set()
    return [d for d in dirs if d and not (d in seen or seen.add(d))]


def _install_stub(stub_dir):
    """Remove any real deepspeed files and write clean stub."""
    # Remove all subpackage dirs/files that the real deepspeed may have left behind.
    # If they remain on disk Python auto-loads them even when __init__.py is our stub,
    # causing _SubmoduleLoader clash and inspect.getfile() crashes in V2.
    if os.path.isdir(stub_dir):
        for entry in os.listdir(stub_dir):
            if entry == '__init__.py':
                continue  # keep, will overwrite below
            entry_path = os.path.join(stub_dir, entry)
            try:
                if os.path.isdir(entry_path):
                    shutil.rmtree(entry_path)
                    print(f'[cleaned] removed {entry_path}')
                else:
                    os.remove(entry_path)
                    print(f'[cleaned] removed {entry_path}')
            except Exception as exc:
                print(f'[warn] could not remove {entry_path}: {exc}')
    else:
        os.makedirs(stub_dir, exist_ok=True)

    init_path = os.path.join(stub_dir, '__init__.py')
    with open(init_path, 'w', encoding='utf-8') as f:
        f.write(STUB_CODE)
    print(f'[ok] deepspeed stub written to {init_path}')


def main():
    # Use purelib (most reliable) as primary location
    sp = sysconfig.get_path('purelib') or os.path.join(sys.prefix, 'Lib', 'site-packages')
    stub_dir = os.path.join(sp, 'deepspeed')
    _install_stub(stub_dir)

    # Also write to venv root for systems that add sys.prefix to sys.path
    venv_root_stub_dir = os.path.join(sys.prefix, 'deepspeed')
    if os.path.abspath(venv_root_stub_dir) != os.path.abspath(stub_dir):
        _install_stub(venv_root_stub_dir)


if __name__ == '__main__':
    main()
