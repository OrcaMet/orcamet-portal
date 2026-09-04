"""
OrcaMet Portal — Map legend colours.

The legend has to show the colours the contour renderer actually uses. It
used to carry a hand-written table of RGB triples that had drifted from the
matplotlib colormaps in map_interpolation.VARIABLE_CMAPS — the wind key, for
instance, showed green at 7 m/s where YlOrRd is pale yellow, so the key was
telling users something the map never said.

The values below are generated from those colormaps, sampled evenly between
each variable's vmin and vmax. They live here as plain data rather than being
computed on request because importing matplotlib into a gunicorn worker to
draw five swatches would cost tens of megabytes of resident memory per
worker, and the whole point of pre-rendering contours in the cron is to keep
matplotlib out of the web process.

Kept honest by forecasts.tests.test_legend_colours, which regenerates them
from the colormaps and fails if this table drifts again.
"""

# Sampled at these fractions of each colormap: 0, 0.25, 0.5, 0.75, 1.
STOP_COUNT = 5

# variable -> (legend title, colormap stops as [value, [r, g, b]], open-ended)
#
# `open_ended` marks a scale whose top stop is a floor rather than a maximum:
# the contour clamps above vmax, so 25 m/s of wind reads "25+", while a
# probability genuinely stops at 100%.
LEGENDS = {
    "pcancel": {
        "title": "Chance of cancellation",
        "unit": "%",
        "open_ended": False,
        "stops": [
            [0.0, [0, 104, 55]],
            [25.0, [135, 203, 103]],
            [50.0, [255, 254, 190]],
            [75.0, [248, 140, 81]],
            [100.0, [165, 0, 38]],
        ],
    },
    "wind": {
        "title": "Wind m/s",
        "unit": "",
        "open_ended": True,
        "stops": [
            [0.0, [255, 255, 204]],
            [6.25, [254, 217, 118]],
            [12.5, [253, 140, 60]],
            [18.75, [226, 25, 28]],
            [25.0, [128, 0, 38]],
        ],
    },
    "gust": {
        "title": "Gusts m/s",
        "unit": "",
        "open_ended": True,
        "stops": [
            [0.0, [255, 255, 204]],
            [8.75, [254, 217, 118]],
            [17.5, [253, 140, 60]],
            [26.25, [226, 25, 28]],
            [35.0, [128, 0, 38]],
        ],
    },
    "precip": {
        "title": "Precip mm/h",
        "unit": "",
        "open_ended": True,
        "stops": [
            [0.0, [247, 251, 255]],
            [2.0, [198, 219, 239]],
            [4.0, [106, 174, 214]],
            [6.0, [32, 112, 180]],
            [8.0, [8, 48, 107]],
        ],
    },
    "temp": {
        "title": "Temp °C",
        "unit": "",
        "open_ended": True,
        "stops": [
            [-5.0, [49, 54, 149]],
            [2.5, [144, 195, 221]],
            [10.0, [255, 254, 190]],
            [17.5, [248, 140, 81]],
            [25.0, [165, 0, 38]],
        ],
    },
}


def _label(value, unit, last, open_ended):
    """A stop's caption: '12.5', '25+', '100%'."""
    text = f"{value:g}"
    if last and open_ended:
        text += "+"
    return text + unit


def legend_data():
    """
    Legend definitions ready for the template, labels included.

    Shaped as the map's own SC table was — {variable: {t, s}} with s a list
    of [value, [r, g, b], label] — so the client keeps rendering it the way
    it always did.
    """
    out = {}
    for variable, spec in LEGENDS.items():
        stops = spec["stops"]
        out[variable] = {
            "t": spec["title"],
            "s": [
                [
                    value,
                    rgb,
                    _label(value, spec["unit"], i == len(stops) - 1,
                           spec["open_ended"]),
                ]
                for i, (value, rgb) in enumerate(stops)
            ],
        }
    return out
