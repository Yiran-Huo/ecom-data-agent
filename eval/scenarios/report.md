# 评测场景报告（自动生成）

> 由 scripts/build_scenarios.py 于 2026-10-21 17:32 生成。共 29 个场景，成立 29 个；纯算法基线在成立的 dev / real 场景里答对 15 / 15 个（测试集只在最后评测时跑一次，这里不显示）。
> 「成立」为 ❌ 的场景评测时会跳过：按提示换日期或调整 specs.json 里的 strength 后重新运行。

| id   | split   | 类型                    | 日期         | 目标                                                             |   强度 | 真实数据当天            | 注入后               | 真实贡献                   | 成立   | 纯算法      |
|:-----|:--------|:----------------------|:-----------|:---------------------------------------------------------------|-----:|:------------------|:------------------|:-----------------------|:-----|:---------|
| d01  | dev     | brand_purchase_drop   | 2019-10-27 | {"brand": "apple"}                                             | 0.5  | -4.1% / z -0.9    | -27.7% / z -6.1   | apple 85%              | ✅    | ✅        |
| d02  | dev     | brand_purchase_drop   | 2019-10-30 | {"brand": "samsung"}                                           | 0.8  | -5.2% / z -0.7    | -19.6% / z -2.7   | samsung 73%            | ✅    | ✅        |
| d03  | dev     | category_traffic_drop | 2019-10-11 | {"level1": "electronics"}                                      | 0.4  | +5.4% / z 0.6     | -23.9% / z -2.8   | electronics 123%       | ✅    | ✅        |
| d04  | dev     | brand_price_cut       | 2019-10-31 | {"brand": "apple"}                                             | 0.45 | -3.2% / z -0.4    | -26.2% / z -3.6   | apple 88%              | ✅    | ✅        |
| d05  | dev     | site_traffic_drop     | 2019-10-12 | {}                                                             | 0.3  | +3.4% / z 0.6     | -25.3% / z -4.3   | —                      | ✅    | ✅        |
| d06  | dev     | new_user_drop         | 2019-10-27 | {"user_type": "new"}                                           | 0.8  | -4.1% / z -0.9    | -22.7% / z -5.0   | new 82%                | ✅    | ✅        |
| d07  | dev     | hour_outage           | 2019-10-09 | {"hours": [13, 14, 15]}                                        |      | -2.7% / z -0.3    | -16.3% / z -1.5   | —                      | ✅    | ✅        |
| d08  | dev     | brand_promo_spike     | 2019-10-31 | {"brand": "samsung"}                                           | 1    | -3.2% / z -0.4    | +15.2% / z 2.1    | samsung 121%           | ✅    | ✅        |
| d09  | dev     | no_anomaly            | 2019-10-10 | {}                                                             |      | +0.6% / z 0.1     | +0.6% / z 0.1     | —                      | ✅    | ✅        |
| d10  | dev     | level2_purchase_drop  | 2019-10-30 | {"level2": "smartphone"}                                       | 0.5  | -5.2% / z -0.7    | -38.8% / z -5.4   | smartphone 87%         | ✅    | ✅        |
| d11  | dev     | no_anomaly            | 2019-10-31 | {}                                                             |      | -3.2% / z -0.4    | -3.2% / z -0.4    | —                      | ✅    | ✅        |
| d12  | dev     | brand_purchase_drop   | 2019-10-12 | {"brand": "apple"}                                             | 0.4  | +3.4% / z 0.6     | -15.0% / z -2.5   | apple 123%             | ✅    | ✅        |
| d13  | dev     | site_traffic_drop     | 2019-10-31 | {}                                                             | 0.2  | -3.2% / z -0.4    | -20.9% / z -2.9   | —                      | ✅    | ✅        |
| d14  | dev     | level2_purchase_drop  | 2019-10-27 | {"level2": "smartphone"}                                       | 0.35 | -4.1% / z -0.9    | -26.8% / z -5.9   | smartphone 85%         | ✅    | ✅        |
| r01  | real    | natural               | 2019-11-15 | {}                                                             |      | -100.0% / z -18.3 | -100.0% / z -18.3 | —                      | ✅    | ✅        |
| t01  | test    | brand_purchase_drop   | 2019-11-07 | {"brand": "samsung"}                                           | 0.9  | +3.1% / z 0.5     | -15.3% / z -2.3   | samsung 120%           | ✅    | （测试集不显示） |
| t02  | test    | category_traffic_drop | 2019-11-09 | {"level1": "electronics"}                                      | 0.5  | -3.7% / z -0.5    | -39.0% / z -5.3   | electronics 90%        | ✅    | （测试集不显示） |
| t03  | test    | brand_price_cut       | 2019-11-05 | {"brand": "apple"}                                             | 0.5  | -0.1% / z -0.0    | -22.6% / z -3.2   | apple 100%             | ✅    | （测试集不显示） |
| t04  | test    | site_traffic_drop     | 2019-11-27 | {}                                                             | 0.25 | +3.2% / z 0.9     | -21.6% / z -5.8   | —                      | ✅    | （测试集不显示） |
| t05  | test    | new_user_drop         | 2019-11-13 | {"user_type": "new"}                                           | 0.85 | -2.3% / z -0.4    | -17.5% / z -3.2   | new 87%                | ✅    | （测试集不显示） |
| t06  | test    | hour_outage           | 2019-11-11 | {"hours": [18, 19, 20]}                                        |      | +2.5% / z 0.5     | -4.5% / z -0.9    | —                      | ✅    | （测试集不显示） |
| t07  | test    | brand_promo_spike     | 2019-11-25 | {"brand": "apple"}                                             | 0.5  | -1.4% / z -0.2    | +23.0% / z 2.6    | apple 106%             | ✅    | （测试集不显示） |
| t08  | test    | no_anomaly            | 2019-11-28 | {}                                                             |      | +0.6% / z 0.2     | +0.6% / z 0.2     | —                      | ✅    | （测试集不显示） |
| t09  | test    | level2_purchase_drop  | 2019-11-26 | {"level2": "smartphone"}                                       | 0.45 | -1.5% / z -0.4    | -33.5% / z -9.1   | smartphone 96%         | ✅    | （测试集不显示） |
| t10  | test    | two_causes            | 2019-11-10 | {"part1": {"brand": "apple"}, "part2": {"brand": "samsung"}}   |      | -4.1% / z -0.8    | -35.1% / z -7.0   | apple 49%, samsung 39% | ✅    | （测试集不显示） |
| t11  | test    | high_price_mix        | 2019-11-14 | {"price_band": "1000+"}                                        | 0.85 | +5.2% / z 1.0     | -16.0% / z -3.1   | 1000+ 132%             | ✅    | （测试集不显示） |
| t12  | test    | no_anomaly            | 2019-11-05 | {}                                                             |      | -0.1% / z -0.0    | -0.1% / z -0.0    | —                      | ✅    | （测试集不显示） |
| t13  | test    | high_price_mix        | 2019-11-28 | {"price_band": "1000+"}                                        | 0.8  | +0.6% / z 0.2     | -19.8% / z -5.1   | 1000+ 103%             | ✅    | （测试集不显示） |
| t14  | test    | two_causes            | 2019-11-13 | {"part1": {"brand": "samsung"}, "part2": {"user_type": "new"}} |      | -2.3% / z -0.4    | -25.9% / z -4.8   | samsung 54%, new 48%   | ✅    | （测试集不显示） |
