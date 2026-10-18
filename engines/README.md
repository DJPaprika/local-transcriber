# engines/

转写引擎层。**目标只有一个：让上层不关心用的是哪个引擎。**

## 接口约定

```python
class Engine(Protocol):
    name: str

    def transcribe(self, wav: Path, *, language: str, quiet: bool = False) -> Transcript:
        ...
```

`Transcript` 是统一的产物结构（定义在 [`base.py`](base.py)）：

```python
@dataclass
class Transcript:
    segments: list[Segment]   # [{start, end, text}, ...] 秒
    text: str                 # 纯文本全文
    lang: str                 # 识别出的语言
    engine: str               # 引擎名
    stats: dict               # 各引擎自己的耗时明细

    def to_srt(self) -> str:  # 由 segments 生成 SRT
```

**所以加一个新引擎，只需要：**

1. 在 `engines/` 下建一个文件，实现一个带 `name` 属性和 `transcribe()` 方法的类
2. 在 [`__init__.py`](__init__.py) 的 `ENGINES` 里注册
3. 在 `transcribe.py` 的 `build_engine()` 里加一个分支

上层什么都不用改。

## 现有两个引擎

| 引擎 | 文件 | 实现方式 | 特点 |
|---|---|---|---|
| `sensevoice` | [`sensevoice.py`](sensevoice.py) | 调 `bridge/sv-bridge.js`（Node，sherpa-onnx） | 快；中文最优；只支持 5 种语言 |
| `whisper` | [`whisper.py`](whisper.py) | 调 `whisper-cli` 子进程 | 慢；多语种；时间戳更精确 |

### sensevoice.py 做了什么

桥只吐**字符级时间戳**（`[[字, 秒], ...]`），把它变成句级字幕的活在这里：

`build_segments()` 按四个条件断句：

| 条件 | 触发 |
|---|---|
| 遇到 `。！？!?…` | 必断 |
| 与下一个字间隔 ≥ 0.45 秒 | 说话有停顿，断 |
| 当前条已跨 8 秒 | 断 |
| 已 24 字且（遇到逗号 或 撑到 30 字） | 断 |

之后再统一修正：每条至少显示 1 秒、且不允许与下一条重叠。

> ⚠️ 已知缺陷：24 字上限是**硬切**，不看词边界，所以会出现"一场长 / 达6个小时"这种断法。
> 接一个中文分词就能解决。

### whisper.py 做了什么

调用 `whisper-cli` 产出 `.srt` 与 `.txt`，然后把 SRT **反解析回 `Segment`**，这样两个引擎的产物才能一致。
同时用正则从日志里捞出各阶段耗时：

```python
TIMING_RE = re.compile(r"whisper_print_timings:\s*([a-z_ ]+?)\s*=\s*([\d.]+)\s*ms")
```

解析出来的 `load_time` 会被上层用来把"模型加载"这项一次性成本从总耗时里扣掉。

## 公共工具：`run_streaming()`

两个引擎都需要跑子进程并计时。它被放在 `base.py` 里，行为是：

1. 子进程的 stdout/stderr **重定向到日志文件**（不用管道——某些受限环境会拒绝创建管道）
2. 主进程每 0.15 秒轮询文件新增内容，转发到终端
3. 返回 `(退出码, 耗时)`

这样既能看到实时进度，又能事后解析完整输出。

```python
code, elapsed = run_streaming(cmd, log_path, quiet=args.quiet)
```

## 加一个引擎的例子

假设要接一个本地的 `paraformer`：

```python
# engines/paraformer.py
from .base import EngineError, Segment, Transcript, run_streaming

class ParaformerEngine:
    name = "paraformer"

    def __init__(self, exe: str):
        self.exe = exe

    def transcribe(self, wav, *, language="zh", quiet=False):
        log_path = wav.with_suffix(".pf.log")
        code, elapsed = run_streaming([self.exe, "-i", str(wav)], log_path, quiet=quiet)
        if code != 0:
            raise EngineError(f"paraformer 退出码 {code}")
        # ... 解析输出、构造 Segment 列表
        return Transcript(segments=..., text=..., lang=language,
                          engine=self.name, stats={"elapsed_seconds": elapsed})
```

然后在 `__init__.py` 里加一行注册即可。
