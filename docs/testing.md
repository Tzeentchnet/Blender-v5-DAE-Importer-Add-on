# Testing

Run all commands from the repository root.

## Pure-Python tests

Run the Blender-independent parser and algorithm tests:

```powershell
python -m unittest discover -s tests\unit -v
```

## Headless Blender tests

Run the same headless integration suite in one or more Blender versions:

```powershell
python tests\run_blender_tests.py `
  --blender "D:\Blender Foundation\Blender 5.2\blender.exe" `
  --blender "D:\Blender Foundation\Blender 5.3\blender.exe"
```

The suite includes export/import round trips for geometry, UV/color
attributes, materials and texture references, cameras/lights, loose edges,
all forward-axis choices, skinning, morphs, native bone attachments, and
bind-pose armature construction.

## Optional asset-corpus smoke tests

An uncommitted local asset corpus can be added as an opt-in smoke test. The
runner selects deterministic small, medium, skinned, and large files unless
`--full-corpus` is supplied:

```powershell
python tests\run_blender_tests.py `
  --blender "C:\Path\To\blender.exe" `
  --corpus-root "D:\Path\To\DAE-Corpus"
```

## Performance benchmarks

Generate a repeatable large mesh/instance benchmark with
`tests\generate_benchmark_fixture.py`; use
`tests\blender\benchmark_import.py` and `tests\compare_benchmarks.py` to compare
scene signatures, peak memory, and wall time against v1.5.3.
