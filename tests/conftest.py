"""Keep native GUI finalization on the test runner's owning thread."""
import gc

import pytest


@pytest.fixture(autouse=True)
def finalize_gui_objects_on_main_thread():
    # A destroyed Tk root can remain in Python reference cycles. If a later
    # research worker's allocations trigger cyclic GC, Tcl finalizers can run on
    # that worker and abort Windows Python. Collect between tests on the same
    # thread that created the GUI, after the other fixtures release references.
    gc.collect()
    yield
    gc.collect()
