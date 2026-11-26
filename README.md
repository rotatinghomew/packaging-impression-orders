# 声明撤回后的拼版委印与包材领用

资料对应一张拼版订单的各位置和下机印次。声明方、印厂、仓班各自提供独立的记录。所有组织、资料、金额和业务回执均为合成。

完整数据口径见[《包装材料与委印记录》](交换记录.md)。输入使用 UTF-8 JSON，资料原件均为 UTF-8 文本。

环境使用 CPython 3.11 标准库，无需安装第三方依赖。下列命令核查交换材料的关联及格式。

```sh
python3 -m unittest discover -s tests -v
```

## 委印服务

生成采购员、仓班、财务和交付证明视图：

```sh
python3 service.py --inputs inputs --out reports
```

也可作为库调用：

```python
from service import load_service

svc = load_service("inputs")
svc.purchasing_report("WL7")       # 应停、已停、超时未停和待收
svc.warehouse_report()             # 按 run/site 的在库、线边、待改贴、可用、已耗用依据
svc.order_availability({           # 销售订单按 market/sku/预计交付日 要料
    "id": "N80", "market": "N", "sku": "P1", "delivery_date": "2026-12-10", "quantity": 100
})
svc.finance_report()               # 合格印件 + 已确认取消费
svc.certificates_report()          # 撤回前交付的当时版号证明
```

标准库 HTTP 适配：`python3 app.py --port 8000`，接口包括 `/api/report`、`/api/purchasing`、`/api/warehouse`、`/api/finance`、`/api/certificates` 和 `POST /api/orders/availability`。

核心口径：

- 版稿按 `(plate, revision)` 精确识别，同名不同版不会落到旧版。
- 相同 `(run, site)` 的印厂完工回执视为同一批实物，不重复增加结算。
- 只有仓班 `received` 增加库存；`issued` 是仓库领用，`delivered` 用于当时版号证明，不再次作为同一批货的仓库出库扣减。
- 遮盖必须同时匹配批准方案与实际 `overlaid` 记录才改变可用用途；批准本身不转换实物。
- 财务按合格印件和 `stop_confirmed` 位置取消费结算；客户撤回不删除已印数量。
