use pyo3::exceptions::PyNotImplementedError;
use pyo3::prelude::*;

#[pyfunction]
fn native_runtime_info() -> &'static str {
    "synthermia_native scaffold"
}

#[pyfunction]
#[pyo3(signature = (_midi_path, _stuck_note_fix_mode="off", _midi_flip=false, _flip_lo=None, _flip_hi=None))]
fn load_packed_playback_source_native(
    _midi_path: &str,
    _stuck_note_fix_mode: &str,
    _midi_flip: bool,
    _flip_lo: Option<u8>,
    _flip_hi: Option<u8>,
) -> PyResult<()> {
    Err(PyNotImplementedError::new_err(
        "native packed MIDI loading is not implemented yet",
    ))
}

#[pymodule]
fn synthermia_native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(native_runtime_info, m)?)?;
    m.add_function(wrap_pyfunction!(load_packed_playback_source_native, m)?)?;
    Ok(())
}
