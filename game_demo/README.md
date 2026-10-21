# 星舰勇者 StarVoyager（Agent 工作台 测试用游戏 Demo）

一个零第三方依赖的竖版射击小游戏（Python 标准库 tkinter 渲染），用于验证
Agent 工作台 的代码索引与中文问答能力。

## 运行

```bash
python main.py
```

方向键 / A、D 左右移动，空格射击，R 键重新开始。

## 模块结构

| 文件 | 职责 |
|---|---|
| `main.py` | 入口：tkinter 窗口、主循环、渲染 |
| `starvoyager/game.py` | 游戏状态机、波次生成、碰撞结算、主更新逻辑 |
| `starvoyager/player.py` | 玩家飞船：移动、射击冷却、受伤与无敌帧 |
| `starvoyager/enemy.py` | 敌人类型（追击者 Chaser / 射手 Shooter）与波次工厂 |
| `starvoyager/bullet.py` | 子弹：移动、出界判定 |
| `starvoyager/collision.py` | 碰撞检测（圆形 / AABB）与命中配对 |
| `starvoyager/score.py` | 计分、连击加成、最高分本地存档 |
| `starvoyager/input.py` | 键盘输入状态 |
| `starvoyager/ui.py` | HUD / 结束界面的绘制指令 |
| `starvoyager/config.py` | 数值加载（读 `config/balance.json`） |
| `starvoyager/utils.py` | clamp 等通用数学工具 |
| `config/balance.json` | 全部平衡性数值（数据驱动） |
| `tests/test_collision.py` | 无依赖自检：`python tests/test_collision.py` |
