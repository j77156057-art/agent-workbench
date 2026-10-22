"""碰撞检测：圆形碰撞、AABB 矩形碰撞，以及子弹与敌人的命中配对。

游戏中所有实体都带 x/y（中心点）与 size（直径或边长）。
"""


def circle_hit(a, b):
    """两个圆形实体是否相交（外接圆碰撞，适合飞船/子弹）。"""
    dx = a.x - b.x
    dy = a.y - b.y
    radius_sum = (a.size + b.size) / 2
    # 距离平方 < 半径和**平方**，避免开平方。
    #
    # ⚠ 半径和必须先平方：漏掉它时判的是「距离平方 < 半径和」，
    # 于是半径和 20 只在距离小于 4.4 时才算命中 —— 绝大多数该撞上的都穿过去了
    # （子弹穿过敌人、敌人撞不到玩家）。自检 `tests/test_collision.py::test_circle_hit`
    # 就是盯着这一条。
    return dx * dx + dy * dy < radius_sum * radius_sum


def aabb_hit(a, b):
    """两个轴对齐矩形是否相交（AABB，适合弹幕/拾取物的宽松判定）。"""
    return (
        abs(a.x - b.x) < (a.size + b.size) / 2
        and abs(a.y - b.y) < (a.size + b.size) / 2
    )


def pair_bullets_enemies(bullets, enemies):
    """找出本帧所有「玩家子弹 ↔ 敌人」的命中对。

    返回 [(bullet, enemy), ...]；同一颗子弹可命中间距极小的多个敌人
    （穿透判定由调用方决定，本游戏在结算时把命中的子弹标记为 dead）。
    """
    hits = []
    for bullet in bullets:
        if bullet.owner != "player" or bullet.dead:
            continue
        for enemy in enemies:
            if enemy.dead:
                continue
            if circle_hit(bullet, enemy):
                hits.append((bullet, enemy))
    return hits


def pair_enemy_bullets_player(bullets, player):
    """找出本帧所有「敌人子弹 ↔ 玩家」的命中。"""
    hits = []
    for bullet in bullets:
        if bullet.owner != "enemy" or bullet.dead:
            continue
        if circle_hit(bullet, player):
            hits.append(bullet)
    return hits


def enemies_reach_player(enemies, player):
    """敌人本体撞到玩家（突击碰撞）。"""
    return [e for e in enemies if not e.dead and circle_hit(e, player)]
