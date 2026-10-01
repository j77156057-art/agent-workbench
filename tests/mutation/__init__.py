"""变异电池（`battery_*.py`）与共用引擎。

这个目录里的东西不是测试用例：`unittest discover -p "test*.py"` 与 pytest 都只会收到
`test_mutation_batteries.py` 那一条【元测试】（查锚点漂移、查层缺失、查引擎自己有没有牙），
真跑变异要显式 `python tests/mutation/run_all.py`。
"""
