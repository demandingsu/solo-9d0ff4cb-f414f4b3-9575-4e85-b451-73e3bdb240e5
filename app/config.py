"""Application configuration."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("WATER_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "water.db"

# Reason codes used on REVIEW rows
REASON_UNPAIRED = "unpaired_reading"
REASON_REVERSED = "reversed_reading"
REASON_CROSS_SWITCH = "crosses_switchover"
REASON_OUTSIDE_WINDOW = "outside_meter_service_window"
REASON_NO_REPLACEMENT = "no_replacement_record"

# Change types on reload-manifest account-day entries
CHANGE_ADDED = "added"                    # 基线无此账户日，当前新出现
CHANGE_REMOVED = "removed"                # 当前已无此账户日，基线中消失
CHANGE_CONSUMPTION = "consumption_changed"       # 用量发生变化
CHANGE_SOURCE = "source_readings_changed"        # 来源读数集合变化（用量未变）
CHANGE_REVIEW_REASON = "review_reason_changed"   # 待核原因变化
CHANGE_DETAIL = "detail_changed"                 # 其余明细变化兜底
