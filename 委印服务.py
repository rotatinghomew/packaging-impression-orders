"""中文模块名兼容入口。"""

from service import *  # noqa: F401,F403
from service import (  # noqa: F401
    CommissionPrintService,
    CommissionPrintingService,
    build_report,
    finance_view,
    load_service,
    main,
    purchasing_view,
    warehouse_view,
)

委印服务 = CommissionPrintingService
