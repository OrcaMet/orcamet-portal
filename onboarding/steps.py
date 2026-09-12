"""
OrcaMet Portal — Onboarding wizard: the shape of the walkthrough.

The step list lives apart from the views so that "which step is next" and
"how far along am I" are answered in one place, by both the views and the
progress strip in the templates.

Progress is a property of the Client row, not of the session: a client who
closes the laptop half way through and comes back tomorrow, on a different
machine, should carry on where they left off rather than start again.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    slug: str
    url_name: str
    title: str
    blurb: str


STEPS = (
    Step("welcome", "onboarding:welcome", "Welcome",
         "What the portal does and how to read it"),
    Step("operation", "onboarding:operation", "Your operation",
         "Contact details and the limits you work to"),
    Step("sites", "onboarding:sites", "Your sites",
         "Add the locations you want forecasts for"),
    Step("thresholds", "onboarding:thresholds", "Check your limits",
         "Adjust the numbers before they start flagging jobs"),
    Step("done", "onboarding:done", "Finish",
         "Head to your dashboard"),
)

STEPS_BY_SLUG = {step.slug: step for step in STEPS}


def progress(current_slug):
    """
    The step list annotated for the progress strip.

    Returns dicts rather than Steps so a template can read `is_current` and
    `is_done` without a custom filter.
    """
    order = [step.slug for step in STEPS]
    try:
        position = order.index(current_slug)
    except ValueError:
        position = 0

    return [
        {
            "number": index + 1,
            "step": step,
            "is_current": index == position,
            "is_done": index < position,
        }
        for index, step in enumerate(STEPS)
    ]
