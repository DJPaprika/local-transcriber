# 摘要示例：24 分钟案件纪录片的转写稿

输入：`docs/examples/bili-demo.srt`（391 条字幕，约 7150 字）
模型：Qwen2.5-7B-Instruct Q4_K_M，`--ctx 16384`，`temperature 0.2`

---

### 一句话结论

史蒂芬麦卡洛因精心策划的谋杀案被判终身监禁。

### 要点

1. 史蒂芬在2022年世界杯决赛夜直播长达6小时，但被怀疑杀害女友娜塔丽。
2. 监控显示案发时一名男子进入娜塔丽家中，但警方最初认为是史蒂芬。
3. 调查发现史蒂芬使用假直播软件，直播内容实为事先录制。
4. 监控显示案发当晚有身份不明男子从史蒂芬住所附近前往娜塔丽家。
5. 史蒂芬在庭审中坚称自己只是直播，但警方发现其手机记录与案发相关。

### 转写存疑处

1. `Is the patient breathing`、`what's the address of the emergency`、`please`
   —— 同一句报警电话被切成了碎片，单独看表达不清。
2. `The 32 year old. Da of violent death. Having been starved in number of times
   And there are sayings of defensive injuries.` —— 疑似语音识别错误：
   `Da of violent death` 应为 `died of violent death`，
   `starved` 应为 `stabbed`（被刺多刀），`sayings` 应为 `signs`（防御性伤痕）。
3. `Ne to kill is. 这才 그단。` —— 句中混入韩文字符 `그단`，属多语言模型的噪声输出。

---

## 这一版和旧版的差别

| | 旧提示词 | 当前提示词 |
|---|---|---|
| 一句话结论 | 60 字，塞了判断和悬念 | 20 字，只留人和结果 |
| 要点 | 8 条，其中第 5、8 条重复 | 5 条，无重复 |
| 第三段 | 「文中提到但没说清的问题」→ 8 条全是「史蒂芬为何要…」的剧情追问 | 「转写存疑处」→ 3 条全是真实的识别错误 |

关键改动只有一处：要求**每条存疑必须附上对应的原文句子**。不附原文时，7B 会顺着"内容还有什么没解释清楚"往下编；附了原文，它的注意力被拉回字面，才看得见 `starved`/`stabbed`、`그단` 这类错误。
