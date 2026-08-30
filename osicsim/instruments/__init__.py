"""Fictional Meridian-series instruments built on the SCPI device base.

Original designs on top of public standards (SCPI-1999 / IEEE-488.2); each
has an authored manual under ``manuals/``. Facts critical to correct use
exist only in those manuals, never in seeds.
"""
from .mer_s240 import MerS240
from .mer_d610 import MerD610
from .mer_p330 import MerP330
from .mer_t115 import MerT115
from .mer_l820 import MerL820
from .mer_g150 import MerG150

REGISTRY = {
    "mer_s240": MerS240,
    "mer_d610": MerD610,
    "mer_p330": MerP330,
    "mer_t115": MerT115,
    "mer_l820": MerL820,
    "mer_g150": MerG150,
}
