# GMV 异动归因 Agent：运行顺序与代码导读

这份文档对应执行计划的 Phase 0–3。代码已经写好，离线测试（合成数据 + 假模型）全部通过；
连数据库、调模型的部分需要在你的电脑上运行，按下面的顺序来。

## 一、运行顺序

每一步跑完先看「检查」那一列，没问题再进行下一步。

| 步骤 | 命令 | 大约耗时 | 检查 |
| --- | --- | --- | --- |
| 1. 抽样 10–11 月 | `python scripts/sample_data.py` | 10–20 分钟 | 用户占比约 10%，两个月都处理了 |
| 2. 导入 MySQL | `python scripts/load_data.py` | 15–30 分钟 | 最后打印 ✅；会删掉旧表重建 |
| 3. 验收 + 数据概况 | `python scripts/verify_db.py` | 1–2 分钟 | 全部 ✅，`agent/data_profile.md` 时间范围到 11-30 |
| 4. 离线测试 | `pytest -q tests/test_analysis.py tests/test_scenarios.py tests/test_diagnose_loop.py` | 1 分钟 | 54 个全部通过（不需要数据库） |
| 5. 旧测试 | `pytest -q` | 1 分钟 | 包括 test_tools.py（需要数据库），全部通过 |
| 6. Phase 0 探查 | `python scripts/explore_daily.py` | 3–10 分钟 | 读 `docs/phase0_report.md`，按建议改 `agent/analysis/config.py` |
| 7. 建评测场景 | `python scripts/build_scenarios.py` | 15–30 分钟 | 读 `eval/scenarios/report.md`：不成立的场景按提示换日期或改强度，再用 `--ids` 重建 |
| 8. 纯算法基线 | `python eval/run_diagnosis_eval.py` | 几分钟 | 记下 Top-1，这是 Agent 要对比的基线 |
| 9. 试一次 Agent | `python cli.py --diagnose 2019-10-16` | 1–2 分钟 | 看计划、工具调用、结论、报告是否合理 |
| 10. Agent 跑 dev | `python eval/run_diagnosis_eval.py --variants v3` | 30–60 分钟 | 看没答对的场景的日志 |

第 6、7 步之间：改了 config.py 的阈值或价格段之后，第 7 步会自动用新参数。
第 7 步需要 `.env` 里的管理员账号有建库和 GRANT 权限（root 可以）。

消融实验和最终测试（Phase 4，先在 dev 上把 v3 调好再跑）：

```bash
python eval/run_diagnosis_eval.py --variants v0 v1 v2 v3 --repeats 3 --tag ablation
python eval/run_diagnosis_eval.py --split test --variants algo v0 v3 --repeats 3 --tag final   # 只跑一次
```

费用提醒：一次排查大约 10–20 轮对话，每轮都带上 System Prompt（DeepSeek 会缓存，便宜很多）。
先用 `--repeats 1` 跑 dev 看效果，`.env` 里填上 `LLM_PRICE_*` 就能在汇总表里看到费用。

## 二、代码导读（建议按这个顺序读）

### 1. 分析工具层 `agent/analysis/`：确定性计算，不调用大模型

| 文件 | 看什么 |
| --- | --- |
| `config.py` | 所有阈值和维度定义都在这里。标了「Phase 0 校准」的要按探查结果调整 |
| `data.py` | `load_window`：一次取「当天 + 前 7 天」的事件放进 pandas，有内存和磁盘两层缓存；`reference_price_band`：价格段按商品参考价划分（为什么见第三节） |
| `metrics.py` | `daily_table`：每天 × 每个元素的 UV、购买用户、购买次数、GMV；`compare`：当天 vs 基线 |
| `anomaly.py` | SOP 第 1 步：变化幅度 + z 分数；按小时检查数据完整性 |
| `decompose.py` | SOP 第 2 步：LMDI。重点看 `lmdi` 怎么保证四项之和 = ΔGMV |
| `attribution.py` | SOP 第 3、4 步：`element_table`、`rank_dimensions`（Adtributor 思路）、`verify_exclusion`。文件开头的说明是面试最可能被问的部分 |
| `pipeline.py` | 纯算法基线：把五步按固定规则串起来，输出和 Agent 一样格式的结论 |

读的时候配合 `tests/test_analysis.py`：每个测试都是「注入一种已知的异动 → 检查工具找到的原因」，
是理解每个函数在做什么最快的方式。合成数据在 `tests/synthetic.py`。

### 2. Agent 层

| 文件 | 看什么 |
| --- | --- |
| `agent/diagnose_tools.py` | 把分析函数包装成「参数 → 文字」的工具，以及给模型看的 JSON Schema。参数错误都返回「错误：……」 |
| `agent/conclusion.py` | 结构化结论的格式和校验。用工具调用交结论，比从文字里解析 ANSWER 行可靠 |
| `agent/prompts_diagnose.py` | v0–v3 四个版本的 prompt 是怎么拼出来的。`python -m agent.prompts_diagnose v3` 可以看完整内容 |
| `agent/diagnose.py` | 主循环。和 `core.py` 结构一样，多了 update_plan、submit_conclusion 两个状态工具和 v3 的护栏 |
| `tests/test_diagnose_loop.py` | 用「假模型」按剧本调用工具，测试护栏、提醒、步数上限 |

### 3. 评测层

| 文件 | 看什么 |
| --- | --- |
| `eval/scenarios/specs.json` | 28 个场景的定义（dev 14 个在 10 月，test 14 个在 11 月；t10、t11、t13、t14 是开发时没见过的类型） |
| `eval/scenario_types.py` | 每种场景类型 = 注入操作 + 标准答案 |
| `eval/scenario_sql.py` | 注入怎么翻译成 MySQL 视图（删除、改价、复制三种写法） |
| `scripts/build_scenarios.py` | 建场景库、算真实贡献、检查场景是否成立、跑纯算法 |
| `eval/scoring.py` | 判分规则：Top-1 定位、误报、因子、多原因召回、贡献误差 |
| `eval/run_diagnosis_eval.py` | 跑评测、出汇总表和消融表 |

### 4. 改动过的旧文件

| 文件 | 改了什么 |
| --- | --- |
| `scripts/sample_data.py` | 同时处理 10 月和 11 月，同一条抽样规则 |
| `scripts/load_data.py` | 分批转换写入（1,100 万行一次性转换会占好几 GB 内存）；生成 `user_first_seen`；清缓存 |
| `sql/schema.sql`、`sql/indexes.sql` | 新增 `user_first_seen` 表；新增 `events(event_time)` 索引 |
| `scripts/verify_db.py` | 检查 `user_first_seen` |
| `agent/db.py` | `get_engine()` + `use_database()`，评测时切换到场景库；兼容旧写法 `from agent.db import engine` |
| `agent/tools.py` | 用当前数据库；describe_table 的注释从真实库读（场景库里是视图，没有注释，Agent 会看出来） |
| `agent/schema_notes.md` | 时间范围改成 10–11 月；新增 user_first_seen 和「归因口径」一节 |
| `eval/questions.jsonl` | 旧题库里写着「10 月」但 SQL 没限定月份的 8 道题，补上 10 月的上界；「最近 7 天」改成 11-24 至 11-30 |
| `cli.py` | 新增 `--diagnose` |

## 三、几个关键设计（面试时要能讲清楚）

1. **为什么分析在 pandas 里做，而不是每一步都写 SQL？**
   归因要反复按不同维度、不同子集算同一批数据。一次取出「当天 + 前 7 天」（约 200 万行），
   之后所有计算都在内存里，每个工具一两秒；而且纯函数可以用合成数据做单元测试。

2. **为什么价格段按参考价划分？**
   按成交价分段时，某品牌降价会让它的商品「跳」到低价段，价格段维度看起来变化最集中，
   算法会把原因错判成价格段。这是合成数据测试里实际发现的问题（`test_price_band_uses_reference_price`）。

3. **「不成比例度」为什么和其余部分比？**
   最初的定义是「占变化的比例 ÷ 占基线 GMV 的比例」。两个原因叠加时，大品牌的这个值被压到 1.4，
   算法判成了全面性变化。改成「自身变化率 ÷ 其余元素的变化率」后两个原因都能找到（`test_two_causes_found_in_two_rounds`），
   而且更好解释：「这个品牌跌了 60%，其余品牌只跌了 27%」。

4. **为什么得分要除以 √(集合大小)？**
   几个元素凑出来的解释，常常混进了随机波动。偏向更简洁的解释，降价场景才不会被高价段的噪声抢走。

5. **为什么评测用视图库？**
   不复制 1,100 万行数据；Agent 看到的表名完全一样，不知道自己在被测；
   run_sql 兜底时查到的也是注入后的数据，和分析工具一致。

6. **为什么要有纯算法基线？**
   纯算法在单一原因的场景上可能已经很准。大模型的价值要体现在规则不擅长的地方：
   多原因、结构变化（高价商品卖少了导致 P 下降）、数据问题判断、能直接给业务看的报告。
   如果 Agent 在某些场景上比纯算法差，这本身就是值得写进 README 的发现。

## 四、和执行计划的差异

- 场景类型 11 从「放大低转化品类的浏览量」改成了 `high_price_mix`（删掉一部分 1,000 以上的购买）：
  同样考察结构效应，但注入方式简单、标准答案明确
- 维度归因多了两条规则（参考价分段、和其余部分比），原因见第三节

## 五、Phase 0 的发现和据此做的调整

1. **11-15 购买埋点缺失（原始数据就是这样）**：Kaggle 原始 11 月文件里，11-15 有 574 万条浏览、48 万条加购，购买 0 条；
   之后 11-16、11-17 的 GMV 是平时的 3 倍和 7 倍。
   - 异常检测新增「按事件类型」的完整性检查（`anomaly.event_gaps`）：某类事件整天接近 0 而其他正常 → 数据问题
   - 把 11-15 作为真实案例 r01 加进评测（`--split real`），标准答案是 data_issue
2. **基线被极端日污染**：11-18 至 11-24 的「前 7 天」包含 0 和 7 倍的日子，和均值比全是 -50%，z 却很小。
   - 新增 `anomaly.baseline_skip_days`：前 7 天里 GMV 低于中位数 30% 或高于 2.5 倍的日子，从基线和 z 分数里剔除
   - 按全站判断，所有工具共用同一组剔除日，各工具的数字才能对得上；工具输出会明确告诉模型剔除了哪几天
3. **真实数据里还有小时级的小缺口**：例如 11-06 0 点只有 80 条事件（平时约 1,100 条）、11-08 1 点只有 71 条（平时约 2,000 条），
   各占全天约 1%。上一版会把它们判成数据问题，导致纯算法在 t01、t02 上直接放弃找原因。
   - 现在缺失的事件合计 ≥ 全天的 5%（`HOUR_GAP_MIN_SHARE`）才算数据问题，更小的缺口只在结论里提一句
   - 小时级基线也跳过极端日（11-17 至 11-22 的「缺口」其实是 11-16、11-17 把小时基线抬高了）
4. **阈值保持 15% / z ≥ 2**：探查脚本上一版建议 75%，是被 11-15 前后的日子带偏了（已修正统计方法）。
   剔除这些日子后，44 个干净日子里只有 4 天被判为异常（10-13、10-14 的 10 月中旬高峰，10-29，11-29 黑五），都是真实的波动
5. **场景重新选日期、调强度**：
   - 日期只用自然波动在 ±5.4% 以内、没有小时缺口的日子；11 月避开 11-06、11-08、11-15 至 11-24 和 11-29
   - apple 占 GMV 50%、samsung 占 21%，其余品牌都在 5% 以下，注入后不可能达到 15% 的阈值，所以品牌类场景只用前两名
   - 二级品类场景改用第 1 名（smartphone），一级品类流量场景改用浏览量第 1 名
   - 强度按「品牌占比 × 强度 ≥ 约 20%」设置；d12、d13 故意设在阈值附近，作为难题

6. **第一次评测的结论**（旧场景，dev 成立的 9 个）：纯算法 89%；v3 有 2 次是 DeepSeek 网络错误（APIConnectionError），
   其余 7 次全部答对。为此：评测遇到网络错误自动重试 2 次；步数上限从 20 提到 24（v3 实际用了 8–19 步）；
   数据问题场景里 is_anomaly 写 true 或 false 都算对（d07 小时故障时 GMV 只变了 +1.4%，Agent 答 false 是合理的）

7. **第二次评测（新场景）发现的问题**：
   - v3 在 d02（samsung 购买全部删除）上判成了数据问题：它用 `brand=samsung` 过滤后检查，工具提示「购买为 0、疑似埋点丢失」。
     子集里没有购买是转化归零（例如断货），不是埋点问题 → 数据完整性现在只在全站层面检查
   - 判分规则漏洞：pattern 答错、但原因写对时也算 Top-1 → 现在原因集中型场景要求 pattern 也对
   - 纯算法在 d13（全站流量 -20%）上把 5 个小时的自然波动当成原因 → 解释集合最多 3 个元素（`MAX_SET_SIZE`），空值元素不能进集合
   - 元素被删空时 LMDI 没有定义，元素因子为空 → 有浏览没购买判为 CVR，用户全没了判为 UV
   - d12 强度 0.35 时只有 -14.5%，差一点到阈值 → 改为 0.4
   - **测试集保护**：建场景的脚本原来会显示纯算法在测试集上的对错（看到了 t02、t04 答错）。
     看了测试集结果再去调算法，测试分数就会虚高。所以：不针对 t02、t04 做任何修改；脚本不再对测试集跑纯算法、不再显示。
     README 里要如实写明「开发过程中曾看到过 2 个测试场景的纯算法结果，未据此修改」

8. **第三次评测：v3 的 3 个「错误」里，有 2 个是 Agent 发现了评测数据的 bug**（纯算法没发现）：
   - d02：Agent 用 SQL 查出 10-30 不只 samsung，「无品牌」商品也是有浏览、有加购、没有购买。
     原因是注入 SQL 的 NULL 三值逻辑：`NOT (... AND p.brand = 'samsung' ...)` 对品牌为空的行结果是 NULL，被 WHERE 过滤掉了，
     当天所有无品牌商品的购买都被误删 → 改用 NULL 安全的 `<=>`，删除条件再包 `COALESCE(..., FALSE)`
     （离线测试没发现，是因为用 pandas 模拟注入时，空值是字符串「(无品牌)」，没有复现 SQL 的 NULL 语义）
   - d06：Agent 发现 6,756 个用户的「首次出现日期」是 10-27，但 10-27 一条事件都没有 —— 现实中不可能。
     原因是场景库的 user_first_seen 直接用了真实库，没有按注入后的数据重算 → 删除用户整天行为的场景，建库时重算这张表
   - d05：这次是 Agent 真的错了：它在 10-12 用新老用户维度下钻，但 10-15 之前新老用户不可靠（左截断），纯算法会跳过这个维度，
     工具却没拦住 Agent → 10-15 之前在任何工具里用 user_type 都直接报错
   - 这是面试里很好的例子：LLM 的价值不只是「答对」，还能在推理过程中发现数据本身的问题

9. **第四次评测：修好上面的 bug 后，d02、d06 还是「错」—— 这次是场景设计的问题**：
   - d02 把 samsung 当天的购买删了 100%：浏览、加购正常，购买正好是 0。Agent 判成「购买埋点丢失」，还联想到 11-15 的真实埋点缺失
   - d06 把当天的新用户删了 100%：user_first_seen 里 10-27 一个新用户都没有，Agent 判成「这张表漏跑了 10-27」
   - 两个判断都站得住：某个分段正好掉到 0，和埋点丢失本来就分不开，真实业务下滑很少会正好到 0
   - 改法：定一条规则 —— 除了 data_issue 类场景，删除比例必须 < 1（建场景时自动检查，`zeroed_segments`）。
     按这条规则改了 d02、d06，以及测试集的 t01、t05、t11、t14（这几个的 strength 也是 1.0）。
     测试集是按规则统一改的，没有看过 Agent 在测试集上的任何结果（README 里会说明）
   - 网络失败（重试完还连不上模型）不再算作答错：单独计数、不计入准确率，并提示单独重跑这几个场景

## 六、还没做的（Phase 4–5）

- 根据第 10 步的失败日志迭代 prompt 和工具输出（Phase 4）
- MCP Server 暴露分析工具：你的 `.venv` 里还没装 `mcp` 包，等 Phase 5 一起做
- 报告配图、Streamlit demo、README
