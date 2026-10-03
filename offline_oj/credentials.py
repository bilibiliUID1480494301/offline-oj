"""账号与口令的**词法与派生**规则。

这个模块只做一件事：回答"用户打的这串东西，和名单上那串是不是同一个"，
以及"由它派生出来的握手索引是什么"。它刻意**不属于任何一层**：

* ``net/session.py`` 需要在握手阶段算出索引（它上线，主机据此找人）、
  并算出共享秘密（据此派生会话密钥）；
* ``core/roster.py`` 需要在导入名单时判断"这个账号是不是重复了"、
  在登录时按账号找人。

``core/`` 不许 import ``net/``（见 ``core/records.py`` 开头那段），
而 ``net/session.py`` 又不许 import ``core/``（它要保住"只依赖标准库"这个属性）。
两边的规则**必须逐字节一致** —— 一旦不一致，现象是"名单上明明有这个账号、
密码也是对的，就是进不去"，而且换台机器可能又好了（取决于哪边被改过）。
这种 bug 比重复一个排序函数危险得多，所以宁可新开一个小模块让两边都 import 它。

为什么归一化必须两端一致
------------------------
索引与密钥都是从"归一化之后的账号"算出来的。学生机算一遍、主机算一遍，
只要一边做了全角转半角而另一边没做，学生打全角数字时就永远进不去。
所以规则只写在这里一份，两端都从这里取。
"""

from __future__ import annotations

import hashlib

__all__ = [
    "ACCOUNT_MAX_LENGTH", "PASSCODE_MAX_LENGTH",
    "normalize_account", "normalize_passcode",
    "credential_secret", "credential_id",
]

#: 账号长度上限。做成一列（学号、拼音名）都不会超，太长只会把界面的输入框撑变形。
ACCOUNT_MAX_LENGTH = 24

#: 口令长度上限。**不是安全下限**，只是别让谁把一段文章粘进来。
PASSCODE_MAX_LENGTH = 32


def normalize_account(text: object) -> str:
    """把用户手打的账号归一化到"同一个账号"的唯一写法。

    只做三件**无歧义**的事：

    1. 去掉所有空白（学生常把 ``2026 031`` 连写或带空格抄）；
    2. 全角字符转半角（中文输入法下极易打出全角数字与字母）；
    3. 统一小写。

    第三步是**故意的**：账号是给人念、给人抄的，"ZhangSan" 与 "zhangsan"
    必须算同一个人，否则名单里会出现两个看起来一样的账号，而其中一个
    永远登不进去。账号不需要靠大小写区分来获得熵（那是口令的事）。

    不做的事：不补零、不截断、不去掉前后缀。位数不对就该报"账号不对"，
    而不是悄悄猜一个 —— 猜错了会让学生以为自己的学号是另一个。
    """
    out: list[str] = []
    for char in str(text or ""):
        if char.isspace() or char == "\u3000":
            continue
        if "\uff01" <= char <= "\uff5e":        # 全角 ！-～ 与半角 ！-～ 一一对应
            char = chr(ord(char) - 0xFEE0)
        out.append(char.lower())
    return "".join(out)


def normalize_passcode(text: object) -> str:
    """口令只去掉**首尾**空白，不做任何折叠。

    与账号相反：口令的熵全靠字符本身，大小写折叠会白白砍掉一大截；
    全角转半角同样会让"老师写在纸条上的那个字"和"程序认的那个字"不是一回事。

    只去首尾空白，是因为从聊天软件复制口令时极易带上一两个空格或换行，
    而那种"看不见的字符导致密码不对"是纯粹的无谓事故。
    """
    return str(text or "").strip()


def credential_secret(account: object, passcode: object,
                      room_password: object = "") -> str:
    """账号 + 个人口令拼成的共享秘密，两端据此派生会话密钥。

    拼法与 :func:`offline_oj.net.session.room_secret` **同构**（一段冒号一段），
    这样两条进场路径在代码里长得一样，读的人不必记两套规则。分隔符用冒号，
    避免"账号 ``a1`` + 口令 ``23``"和"账号 ``a12`` + 口令 ``3``"撞到一起。

    :param room_password: 本场**全场口令**。非空时接在最后一段 —— 于是
        "账号模式 + 全场口令"成了双因子：两个都对才推导得出同一把钥匙。
        名字里带 ``room_`` 是为了读代码的人一眼看出它是全场共用的那个，
        而不是某个人的。
    """
    secret = f"{normalize_account(account)}:{normalize_passcode(passcode)}"
    extra = normalize_passcode(room_password)
    return f"{secret}:{extra}" if extra else secret


def credential_id(account: object, passcode: object,
                  room_password: object = "") -> str:
    """账号秘密的单向索引，握手时明文上线的是它，而不是账号。

    与 :func:`offline_oj.net.session.room_id` 同样是"不主动泄密"而非"防爆破"：
    拿到索引后可以离线穷举账号口令去比对。真正的加固是让口令长一点、
    以及让主机对握手做速率限制（``server.py`` 已有）。

    与房间索引的空间**天然有重叠**（两者都是 ``sha256(一段:一段)``），
    但这不造成混淆：主机只会按自己这一场的进场方式去查对应的那一侧。
    """
    return hashlib.sha256(
        credential_secret(account, passcode, room_password).encode("utf-8")
    ).hexdigest()[:16]
