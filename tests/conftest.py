# -*- coding: utf-8 -*-
"""pytest ayarı: repo kökünü import yoluna ekler ki testler
'import takip_botu_pro' yapabilsin."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
