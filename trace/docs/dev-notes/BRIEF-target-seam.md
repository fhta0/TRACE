# 任务：Target 适配器接缝重构（纯重构，零行为改动）

目标：把"驱动被测桌面智能体"的逻辑从写死 `WorkBuddyTarget` 改成**可插拔适配器**，让以后接入任意桌面智能体 = 写一个新适配器 + 注册一行。沙箱固定用 AgentBay（`session` 对象不变），本次**不加新 agent、不改判定逻辑**。

## 背景：现有的隐式接口

`runner.py` 当前只调用 Target 的两个方法（这就是接口的全部）：
- `target.plant_doc(filename: str, content: str) -> str` —— 把注入文档投放到位
- `target.dispatch(benign_task: str, wait_seconds: int) -> bytes` —— 新建→输入→发送→等待→截图，返回 PNG bytes

`__init__(self, session)` 接收一个 AgentBay session 对象。

## 要做的改动

### 1. 新建 `trace/target.py`：抽象基类 + 工厂

```python
"""Target 适配器接口：把'驱动某个桌面智能体'抽象出来，按 case['target'] 选择具体实现。
沙箱固定为 AgentBay（session 对象由 provider 提供）。接入新智能体 = 新增一个 Target 子类并在 get_target 注册。"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class Target(ABC):
    """一个被测桌面智能体的驱动适配器。子类实现如何投放注入、如何下发任务。"""

    def __init__(self, session: Any):
        self.session = session

    @abstractmethod
    def plant_doc(self, filename: str, content: str) -> str:
        """把注入文档投放到智能体能读到的位置，返回完整路径。"""
        raise NotImplementedError

    @abstractmethod
    def dispatch(self, benign_task: str, wait_seconds: int) -> bytes:
        """向智能体下发一个正常任务，等待其执行，返回一张截图的 PNG bytes。"""
        raise NotImplementedError


def get_target(name: str, session: Any) -> Target:
    """按 case['target'] 选择适配器。懒加载具体实现，避免循环导入。"""
    if name == "workbuddy":
        from .target_workbuddy import WorkBuddyTarget
        return WorkBuddyTarget(session)
    raise NotImplementedError(f"未知的 target: {name!r}（当前仅支持 'workbuddy'）")
```

### 2. `trace/target_workbuddy.py`：让 `WorkBuddyTarget` 继承 `Target`

- `from .target import Target`
- `class WorkBuddyTarget(Target):`
- **保留全部现有方法和实现不变**（坐标 77,107 / 1075,455 / 1524,500、dispatch 序列、plant_doc、screenshot 等一律不动）。
- 由于基类 `__init__` 已存 `self.session`，可删掉子类里重复的 `__init__`（若删了要确认 `self.session` 仍来自基类）；不确定就保留子类 `__init__` 也行，但要 `super().__init__(session)`。

### 3. `trace/runner.py`：用工厂替换写死的构造

- 顶部：把 `from .target_workbuddy import WorkBuddyTarget` 换成 `from .target import get_target`。
- `run_case` 里：删掉 `if case.get("target") != "workbuddy": raise NotImplementedError(...)` 这个硬判断（工厂现在负责未知 target 报错）。**保留** vector 的判断（仍只支持 `doc_injection`）。
- 把 `target = WorkBuddyTarget(session)` 改成 `target = get_target(case["target"], session)`。
- 类型标注里 `WorkBuddyTarget` 若有出现，改成 `Target`（从 `.target` 导入）或直接用 `Any`。

### 4. 文档：README 加一节"接入新桌面智能体"

在 `README.md` 末尾（第 8 节之后）加一小节 `## 9. 接入新的桌面智能体`，简述：
- 沙箱固定 AgentBay，接入新 agent 只需写一个 `Target` 子类，实现两个方法 `plant_doc` / `dispatch`（说明各自职责）；
- 在 `trace/target.py` 的 `get_target` 里注册一行（`if name == "<新名字>": ...`）；
- case.json 的 `target` 字段填新名字即可；
- 当前 `dispatch` 靠手标坐标驱动 UI，接入新 agent 需标定其输入框/发送键坐标（动态 UI 定位在后续版本）；安装/登录目前仍是手动前置（同 README 第 3 节）。

## 约束（严格）

- **纯重构，WorkBuddy 的实际驱动行为零改动**：坐标、dispatch 序列、截图方式全部保持。
- **不碰** `oracle.py` / `provider.py` / `cli.py` / `report.py` 的逻辑；不改判定逻辑；不改 CONTRACT 的 schema 语义。
- 不运行工具、不 pip install、不发网络请求。
- 改完打印：
  1. 新增/修改的文件列表；
  2. `Target` 基类的两个抽象方法签名；
  3. `get_target` 的分发分支；
  4. 确认 `WorkBuddyTarget` 的坐标常量和 dispatch 序列未改动（把这几行贴出来自证）。
