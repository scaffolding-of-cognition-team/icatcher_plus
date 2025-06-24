### define version
__version__ = "0.2.3"
version = __version__
### define classes
#classes = {"none": -3, "noface": -2, "nobabyface": -1, "away": 0, "left": 1, "right": 2}
classes = {"none": -3, "noface": -2, "nobabyface": -1, "away": 0, "center": 1, "left": 2, "right": 3}
reverse_classes = {v: k for k, v in classes.items()}
### imports
from . import (
    draw,
    options,
    parsers,
    video,
    models,
    cli,
    face_detector,
)
