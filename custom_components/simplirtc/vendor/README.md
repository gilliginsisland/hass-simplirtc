Vendored upstream dependencies used by SimpliRTC.

`aiortc` is vendored from upstream version 1.14.0 so Home Assistant can provide
a compatible `av` package without also installing aiortc's stricter package
metadata. The vendored source is otherwise upstream aiortc code with imports
namespaced under `custom_components.simplirtc.vendor`.
