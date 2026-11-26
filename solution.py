"""兼容常见命名的服务入口。

测试或调用方既可以 ``from service import CommissionPrintingService``，
也可以 ``from solution import CommissionPrintingService, build_report``。
"""

from service import *  # noqa: F401,F403
from service import (  # noqa: F401
    CN_TZ,
    CommissionPrintingService,
    DuplicateEventError,
    ServiceError,
    UnknownProofError,
    build_report,
    certificates_view,
    finance_view,
    jsonable,
    load_json,
    load_service,
    main,
    purchasing_view,
    warehouse_view,
    write_reports,
)

PrintService = CommissionPrintingService
PrintingService = CommissionPrintingService
Solution = CommissionPrintingService
create_service = load_service
run = main

if __name__ == "__main__":
    raise SystemExit(main())
