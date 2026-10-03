"""雷同检测（``core/similarity.py``）的测试。

这一层的风险不是"算不出来"，而是**算出一个错误结论**，三个方向都会有后果：

* **少归一化一点** → 改个名就躲掉了（漏报，老师白查一场）；
* **多归一化一点** → 两段不同的代码被判成一样（误报，而它直接指向某个学生）；
* **骨架扣多了** → 全班两两相似度归零 —— 这是最坏的一种：报告看起来
  "一切正常"，而真相被这个假象盖住了。

所以断言分四组：**词法层**（什么算注释、什么算同一个 token）、
**判定层**（谁和谁被判相似）、**报告层**（口径与边界）、
**分层守卫**（``core/`` 不许 import 界面与网络）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from offline_oj.core import similarity as sim

ROOT = Path(__file__).resolve().parent.parent

# ======================================================================
# 样例代码
# ======================================================================

#: 一份"全班都会写"的骨架。刻意写得够长（token 数远超 k=15），
#: 否则短代码走的是"整串一个指纹"的退化分支，测不到骨架扣除。
TEMPLATE = """\
#include <bits/stdc++.h>
using namespace std;
int main() {
    int n;
    scanf("%d", &n);
    printf("%d\\n", solve(n));
    return 0;
}
"""

#: 一段有特征的个人解法（累加法）。
LOOP_SUM = """\
int solve(int n) {
    int total = 0;
    for (int i = 1; i <= n; i++) total += i;
    return total;
}
"""

#: 同一段逻辑，只把名字全换掉 —— 归一化之后应当与 :data:`LOOP_SUM` 一致。
LOOP_SUM_RENAMED = """\
int calc(int m) {
    int sum = 0;
    for (int j = 1; j <= m; j++) sum += j;
    return sum;
}
"""

#: 另一种真实的独立解法（公式法），不该与累加法相似。
FORMULA_SUM = """\
int quick(int n) {
    return n * (n + 1) / 2;
}
"""


def row(serial: int, code: str, *, problem: str = "P0001", who: str = "",
        device: str = "", language: str = "cpp", score: int = 100) -> dict:
    """造一条待比对的记录（界面那侧会把 Submission 转成这个形状）。"""
    return {
        "serial": serial,
        "device_id": device or f"DEV{serial:05d}",
        "username": who or f"选手{serial}",
        "problem_id": problem,
        "language": language,
        "code": code,
        "score": score,
    }


# ======================================================================
# 词法层：什么算注释，什么算字符串
# ======================================================================


def test_line_comment_is_stripped():
    assert sim.strip_comments("int a = 1; // 加个一\n", "cpp") == "int a = 1;"


def test_block_comment_is_stripped():
    code = "int a = 1;\n/* 多行\n   注释 */\nint b = 2;\n"
    assert sim.strip_comments(code, "cpp") == "int a = 1;\nint b = 2;"


def test_python_hash_comment_is_stripped():
    assert sim.strip_comments("x = 1  # 注释\n", "python") == "x = 1"


def test_double_slash_inside_string_is_not_a_comment():
    """``"http://x"`` 里的 ``//`` 不是注释。

    这是最容易写错的一条：用正则去注释的实现几乎必错，而错法很隐蔽 ——
    字符串后半段被当注释切掉，之后所有比对都在残缺的代码上做。
    """
    code = 'printf("http://example.com");\n'
    assert sim.strip_comments(code, "cpp") == 'printf("http://example.com");'
    assert sim.normalized_tokens(code, "cpp") == \
        sim.normalized_tokens('printf("https://other.org");', "cpp")


def test_hash_inside_string_is_not_a_comment():
    code = 's = "# 这不是注释"\n'
    assert sim.strip_comments(code, "python") == 's = "# 这不是注释"'


def test_escaped_quote_does_not_end_the_string():
    code = 's = "he said \\"hi\\" // not a comment"\n'
    assert "not a comment" in sim.strip_comments(code, "cpp")


def test_triple_quoted_string_is_one_string_token():
    """Python 三引号里的 ``#`` 同样不是注释，而且整块只算一个字符串 token。"""
    code = 'doc = """第一行\n# 井号在字符串里\n第三行"""\nx = 1\n'
    assert sim.normalized_tokens(code, "python") == ["$ID", "=", "$STR", "$ID", "=", "$NUM"]
    assert sim.strip_comments(code, "python").count("\n") == 3


def test_python_string_prefix_is_not_split_into_identifier_and_string():
    """``r"…"`` / ``f"…"`` 要整体当字符串，不能切成"标识符 + 字符串"。"""
    plain = sim.normalized_tokens('x = "abc"', "python")
    raw = sim.normalized_tokens('x = r"abc"', "python")
    assert plain == raw


def test_unterminated_string_does_not_swallow_the_rest_of_the_file():
    """未闭合的引号只吃到行尾 —— 否则一个笔误会让整份代码变成一整个字符串，
    与别人的相似度变成 0（看着像"他独立写的"）。"""
    code = 'int a = 1;\nchar *s = "忘了闭合\nint b = 2;\n'
    assert "int b = 2" in sim.strip_comments(code, "cpp")


def test_bom_and_crlf_do_not_change_the_digest():
    """带 BOM / CRLF 的文件与干净的 LF 文件是同一份代码。

    学生从 Windows 记事本另存出来的就是带 BOM 的 CRLF —— 不该因此判成两份。
    """
    clean = "int main() {\n    return 0;\n}\n"
    dirty = "\ufeffint main() {\r\n    return 0;\r\n}\r\n"
    assert sim.exact_digest(clean, "cpp") == sim.exact_digest(dirty, "cpp")


# ======================================================================
# 归一化层：什么被抹平，什么必须留下
# ======================================================================


def test_renamed_identifiers_normalize_to_the_same_tokens():
    assert sim.normalized_tokens(LOOP_SUM, "cpp") == \
        sim.normalized_tokens(LOOP_SUM_RENAMED, "cpp")


def test_literal_values_are_erased():
    """常量改一改不算改了算法 —— ``n * (n + 1) / 2`` 与 ``n * (n + 3) / 2``
    在词法上确实一样，这是"改数据不改逻辑"的常见躲法。"""
    left = "int f(int n) { return n * (n + 1) / 2; }"
    right = "int f(int n) { return n * (n + 3) / 2; }"
    assert sim.normalized_tokens(left, "cpp") == sim.normalized_tokens(right, "cpp")


def test_keywords_are_not_normalized():
    """``for`` 换成 ``while`` 是**真的改了**，不能被抹平。"""
    left = "for (int i = 0; i < n; i++) total += i;"
    right = "while (int i = 0; i < n; i++) total += i;"
    assert sim.normalized_tokens(left, "cpp") != sim.normalized_tokens(right, "cpp")


def test_python_keywords_are_not_normalized():
    left = "for i in range(n):\n    x += i"
    right = "if i in range(n):\n    x += i"
    assert sim.normalized_tokens(left, "python") != \
        sim.normalized_tokens(right, "python")


def test_builtin_names_are_not_normalized():
    """``print`` 与 ``foo`` 都塌成 ``$ID`` 的话，"换了函数名"会显得更像。"""
    left = "print(a)"
    right = "emit(a)"
    assert sim.normalized_tokens(left, "python") != \
        sim.normalized_tokens(right, "python")


def test_structure_is_preserved():
    """控制结构变了就是变了：``if`` 体去掉一个分支，token 序列必须不同。"""
    left = "if (x) { a(); } else { b(); }"
    right = "if (x) { a(); }"
    assert sim.normalized_tokens(left, "cpp") != sim.normalized_tokens(right, "cpp")


# ======================================================================
# 完全重复：确定性的那一层
# ======================================================================


def test_comment_and_blank_line_differences_are_the_same_code():
    left = "int a = 1;\n\n// 我加的注释\nint b = 2;\n"
    right = "int a = 1;\nint b = 2;\n"
    assert sim.exact_digest(left, "cpp") == sim.exact_digest(right, "cpp")


def test_inline_whitespace_is_squeezed():
    """``int  main()`` 与 ``int main()`` 是同一段代码（纯排版）。"""
    assert sim.exact_digest("int  main() {\n    return 0;\n}", "cpp") == \
        sim.exact_digest("int main() {\n\treturn 0;\n}", "cpp")


def test_c_indent_width_is_irrelevant():
    """C 系里缩进无语义，2 格还是 4 格是同一段代码。"""
    assert sim.exact_digest("int main() {\n  int a = 1;\n}", "cpp") == \
        sim.exact_digest("int main() {\n\t\tint a = 1;\n}", "cpp")


def test_python_indent_is_significant():
    """**Python 里缩进是语法**：``if x:\\n    y`` 是一个块，``if x:\\ny`` 是
    语法错误。把它们归一成同一个东西就是误报 —— 这一层承诺零误报。"""
    block = "if x:\n    y = 1\n"
    flat = "if x:\ny = 1\n"
    assert sim.exact_digest(block, "python") != sim.exact_digest(flat, "python")


def test_python_deeper_indent_differs():
    """缩进深浅不同（块的层级不同）也要判成不同。"""
    shallow = "if x:\n    y = 1\n"
    deeper = "if x:\n        y = 1\n"
    assert sim.exact_digest(shallow, "python") != sim.exact_digest(deeper, "python")


def test_python_inline_whitespace_is_still_squeezed():
    """行内空白在 Python 里同样无语义，照常压掉。"""
    assert sim.exact_digest("x  =  1\n", "python") == \
        sim.exact_digest("x = 1\n", "python")


def test_real_change_is_not_reported_as_duplicate():
    left = "int total = 0;\nfor (int i = 1; i <= n; i++) total += i;"
    right = "int total = 0;\nfor (int i = 1; i <= n; i++) total *= i;"
    assert sim.exact_digest(left, "cpp") != sim.exact_digest(right, "cpp")


def test_duplicate_groups_only_within_the_same_problem():
    """同样的代码交到两道不同的题上，不该被算成一个"完全重复组"。"""
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, problem="P0001", who="甲"),
        row(2, TEMPLATE + LOOP_SUM, problem="P0002", who="乙"),
    ])
    assert report.exact_groups == []


# ======================================================================
# 指纹与骨架
# ======================================================================


def test_short_code_still_produces_a_fingerprint():
    """比 k 还短的代码（一行题）不能给出空集合 —— 空集合对什么都是 0%。"""
    assert sim.fingerprints("int main(){}", "cpp")


def test_fingerprints_ignore_renaming():
    assert sim.fingerprints(LOOP_SUM, "cpp") == \
        sim.fingerprints(LOOP_SUM_RENAMED, "cpp")


def test_fingerprints_are_stable_across_calls():
    """指纹不能带进程随机化 —— 否则同一份代码两次跑出不同结果。"""
    assert sim.fingerprints(TEMPLATE + LOOP_SUM, "cpp") == \
        sim.fingerprints(TEMPLATE + LOOP_SUM, "cpp")


def test_common_template_is_deducted_when_sample_is_large_enough():
    """三人以上都写的骨架要被扣掉：扣完谁都只剩自己的解法，两两不再相似。"""
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + FORMULA_SUM, who="乙"),
        row(3, TEMPLATE + "int w(int n) { return -n; }", who="丙"),
    ], min_similarity=0.0)
    assert report.pairs == []


def test_identical_whole_class_is_caught_by_the_exact_layer():
    """全班十二份代码一模一样时的兜底：骨架扣除会把所有指纹都扣掉
    （它们确实"全班都有"），相似对一条都不剩 —— 但**完全重复层不依赖
    骨架扣除**，它会把这一组直接摆出来。这两层分开的价值就在这里。
    """
    rows = [row(index + 1, TEMPLATE + LOOP_SUM, who=f"选手{index}")
            for index in range(12)]
    report = sim.analyse(rows, min_similarity=0.0)
    assert len(report.exact_groups) == 1
    assert len(report.exact_groups[0].members) == 12


def test_template_deduction_does_not_eat_the_real_similarity():
    """扣骨架的同时，两个人**私有的**那段相似必须留下来 —— 否则这个功能就废了。

    甲、乙在骨架之外还共享一段累加解法（只是改了名），丙、丁各写各的。
    骨架被扣掉之后，甲乙仍然要被打出来。
    """
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM_RENAMED, who="乙"),
        row(3, TEMPLATE + FORMULA_SUM, who="丙"),
        row(4, TEMPLATE + "int w(int n) { return -n; }", who="丁"),
    ], min_similarity=0.0)
    pairs = {(pair.left.display, pair.right.display): pair
             for pair in report.pairs}
    assert ("甲", "乙") in pairs, sorted(pairs)
    assert pairs[("甲", "乙")].similarity > 0.99
    assert ("甲", "丙") not in pairs
    assert ("甲", "丁") not in pairs


# ======================================================================
# 判定层：谁和谁被判相似
# ======================================================================


def test_renamed_copy_is_reported_as_highly_similar_but_not_exact():
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM_RENAMED, who="乙"),
    ])
    assert len(report.pairs) == 1
    pair = report.pairs[0]
    assert pair.similarity > 0.99
    assert pair.band == "high"
    assert pair.exact is False, "改了名字就不该标成完全重复"
    assert {pair.left.display, pair.right.display} == {"甲", "乙"}


def test_exact_copy_is_flagged_on_the_pair():
    """一模一样的代码既进"完全重复组"，也要在对里标明 ——
    否则它会以 96% 的身份混在"高度相似"里，而那是两种性质的事。"""
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM + "// 加个注释\n", who="乙"),
    ])
    assert len(report.exact_groups) == 1
    assert report.exact_groups[0].names == ["甲", "乙"]
    assert report.pairs and report.pairs[0].exact is True


def test_independent_solutions_are_not_reported():
    """三人以上时骨架被扣掉，两种截然不同的解法互不相干。

    特意用**三份**：两份提交时公共模板不会被扣（见
    :func:`test_two_samples_keep_the_template_noise`），那时连独立解法之间
    也会因为共享骨架而有四五成的相似度。
    """
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + FORMULA_SUM, who="乙"),
        row(3, TEMPLATE + "int w(int n) { return -n; }", who="丙"),
    ], min_similarity=0.0)
    assert report.pairs == [], "骨架扣掉之后，三种不同解法之间不该有相似"


def test_two_samples_keep_the_template_noise():
    """**两份提交时不扣骨架**，这是刻意的取舍，代价写在这里免得将来被当成 bug：

    样本只有两份时，"两份都有"的公开度是 100%，照比例扣的话会把两人
    **真正共享的那段代码也一起扣掉** —— 抄的人被判 0，是最坏的一种错。
    宁可留噪声（公共模板把相似度整体抬高），也不能漏掉真结论。
    报告里会有一条 note 明确说明这一点。
    """
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + FORMULA_SUM, who="乙"),
    ], min_similarity=0.0)
    assert report.pairs, "两份时不扣骨架，所以仍会有（被模板抬高的）相似度"
    assert report.pairs[0].similarity < 0.6, "但也不该高到吓人"
    assert any("没能自动扣除公共模板" in note for note in report.notes)
    assert any("请只当作线索" in note for note in report.notes)


def test_renamed_copy_is_still_caught_with_only_two_samples():
    """小样本不扣骨架的真正原因：**别把两人共享的那段也扣掉**。

    甲、乙都把骨架 + 同一段累加解法（只改了名）交上来。这时三份样本那种
    "扣骨架"的做法会把两段代码一起抹平，相似度归零 —— 那才是致命的。
    """
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM_RENAMED, who="乙"),
    ], min_similarity=0.0)
    assert len(report.pairs) == 1
    assert report.pairs[0].similarity > 0.99


def test_different_problems_are_never_compared():
    """跨题比较没有意义：两道题都写 ``for`` 循环不是抄。"""
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, problem="P0001", who="甲"),
        row(2, TEMPLATE + LOOP_SUM, problem="P0002", who="乙"),
    ], min_similarity=0.0)
    assert report.pairs == []


def test_similar_solutions_on_two_problems_collapse_to_the_best_one():
    """同一对人若在两道题上都像，只报他们最像的那一道 —— 否则 20 人的班
    在 3 道题上都像，条目会翻三倍，而老师想看的只是"谁和谁最像"。

    注意两份记录要显式给同一个 ``device_id`` —— 真实的 session 里同一个人
    的每份提交都带同一个设备号，而"人"的身份正是靠它认的。
    """
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, problem="P0001", who="甲", device="AAAA"),
        row(2, TEMPLATE + LOOP_SUM, problem="P0001", who="乙", device="BBBB"),
        # 第二道题上只有前半段一样（相似度更低）
        row(3, TEMPLATE + LOOP_SUM, problem="P0002", who="甲", device="AAAA"),
        row(4, TEMPLATE + LOOP_SUM + FORMULA_SUM, problem="P0002", who="乙",
            device="BBBB"),
    ], min_similarity=0.0)
    assert len(report.pairs) == 1
    assert report.pairs[0].problem_id == "P0001"


# ======================================================================
# 代表人挑选
# ======================================================================


def test_representative_is_the_highest_scoring_submission():
    """同一人同一题交了好几次时取**最高分**那次 —— 抄来的那份通常就是他
    得分最高的那一版，而最后交的可能是试错到一半的半成品。"""
    report = sim.analyse([
        row(1, "int a(){return 1;}", who="甲", score=30, device="AAAA"),
        row(2, "int a(){return 2;}", who="甲", score=100, device="AAAA"),
    ], min_similarity=0.0)
    assert [person.serial for person in report.analysed] == [2]
    assert report.merged == 1


def test_tie_on_score_takes_the_later_submission():
    report = sim.analyse([
        row(1, "int a(){return 1;}", who="甲", score=100, device="AAAA"),
        row(2, "int a(){return 2;}", who="甲", score=100, device="AAAA"),
    ], min_similarity=0.0)
    assert [person.serial for person in report.analysed] == [2]


def test_different_people_are_not_merged():
    report = sim.analyse([
        row(1, "int a(){return 1;}", who="甲", device="AAAA"),
        row(2, "int b(){return 2;}", who="乙", device="BBBB"),
    ], min_similarity=0.0)
    assert len(report.analysed) == 2
    assert report.merged == 0


def test_merged_count_is_reported_so_the_numbers_add_up():
    """报告里出现的份数必须能解释清楚 —— 老师会数"我明明收了 40 份"。"""
    report = sim.analyse([
        row(1, "int a(){return 1;}", who="甲", device="AAAA"),
        row(2, "int a(){return 2;}", who="甲", device="AAAA"),
        row(3, "int b(){return 3;}", who="乙", device="BBBB"),
    ], min_similarity=0.0)
    assert len(report.analysed) == 2
    assert report.merged == 1


# ======================================================================
# 边界与报告口径
# ======================================================================


def test_submission_without_code_is_reported_not_crashed():
    """档案可以勾「只存成绩不存代码」，那时提交还在、源码没了。"""
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, "", who="乙"),
    ])
    assert len(report.analysed) == 1
    assert len(report.missing_code) == 1
    assert "乙" in report.missing_code[0]


def test_report_carries_the_code_for_the_side_by_side_diff():
    """并排 diff 要用源码，而源码不能挂在 :class:`Person` 上 ——
    同一个人在不同题目里的源码不同，挂上去会被后一道题覆盖。"""
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM_RENAMED, who="乙"),
    ])
    assert report.codes[1] == TEMPLATE + LOOP_SUM
    assert report.codes[2] == TEMPLATE + LOOP_SUM_RENAMED


def test_empty_input_gives_an_empty_report():
    report = sim.analyse([])
    assert report.is_empty
    assert report.analysed == []


def test_single_submission_has_nothing_to_compare():
    report = sim.analyse([row(1, TEMPLATE + LOOP_SUM, who="甲")])
    assert report.pairs == []
    assert report.exact_groups == []


def test_min_similarity_filters_the_report():
    rows = [
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM_RENAMED, who="乙"),
    ]
    assert sim.analyse(rows, min_similarity=0.99).pairs
    assert sim.analyse(rows, min_similarity=1.01).pairs == []


#: 六段**结构互不相同**的私有代码。刻意不用"同一个函数换个名字/换个常量"
#: 的写法 —— 那种差异会被词法归一化抹平，六份提交就变成同一份了。
PRIVATE_PARTS = (
    "int p0(int a) { while (a > 0) a--; return a; }",
    "int p1(int a) { if (a) return a; return 0; }",
    "int p2(int a) { switch (a) { case 1: return 1; } return 2; }",
    "int p3(int a) { do { a++; } while (a < 5); return a; }",
    "int p4(int a) { for (int i = 0; i < a; i++) a -= i; return a; }",
    "int p5(int a) { return a ? a : -a; }",
)


def test_top_pairs_limits_the_report():
    """六对互不相干的相似提交，只报最高的三对。

    每组两人共享"骨架 + 累加解法 + 一段私有代码"，组与组之间**只**共享
    骨架与累加解法 —— 骨架和累加解法在十二份里都出现，会被当模板扣掉，
    于是每组剩下的就是那对私有指纹。这样才测得到"排序 + 截断"本身。
    """
    rows = []
    for index, private in enumerate(PRIVATE_PARTS):
        rows.append(row(index * 2 + 1, TEMPLATE + LOOP_SUM + private,
                        who=f"甲{index}"))
        rows.append(row(index * 2 + 2, TEMPLATE + LOOP_SUM_RENAMED + private,
                        who=f"乙{index}"))
    report = sim.analyse(rows, min_similarity=0.0, top_pairs=3)
    assert len(report.pairs) == 3
    assert all(pair.similarity > 0.99 for pair in report.pairs)


def test_pairs_are_sorted_by_similarity_descending():
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM_RENAMED, who="乙"),
        row(3, TEMPLATE + LOOP_SUM + FORMULA_SUM, who="丙"),
    ], min_similarity=0.0)
    scores = [pair.similarity for pair in report.pairs]
    assert scores == sorted(scores, reverse=True)


def test_notes_always_say_manual_review_is_required():
    """「供人工复核」不是免责声明，是这个功能的正确用法 —— 报告里必须常驻。"""
    report = sim.analyse([row(1, TEMPLATE + LOOP_SUM, who="甲")])
    assert any("人工复核" in note for note in report.notes)
    assert any("同一道题" in note for note in report.notes)
    assert any("模板" in note for note in report.notes)


def test_notes_explain_merging_and_missing_code():
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲", device="AAAA"),
        row(2, TEMPLATE + LOOP_SUM, who="甲", device="AAAA"),
        row(3, "", who="乙"),
    ])
    joined = "\n".join(report.notes)
    assert "合并" in joined
    assert "没有源码" in joined


def test_matrix_is_symmetric_with_unit_diagonal():
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM_RENAMED, who="乙"),
        row(3, TEMPLATE + FORMULA_SUM, who="丙"),
    ], min_similarity=0.0)
    matrix = report.matrices["P0001"]
    assert len(matrix.people) == 3
    for i, line in enumerate(matrix.values):
        assert line[i] == 1.0
        for j in range(len(line)):
            assert line[j] == matrix.values[j][i]


def test_matrix_follows_the_submission_order():
    """矩阵行序按**提交编号**（交卷顺序），不按名字 ——
    按中文名排序只会让报告看起来随机。"""
    report = sim.analyse([
        row(1, TEMPLATE + LOOP_SUM, who="甲"),
        row(2, TEMPLATE + LOOP_SUM_RENAMED, who="乙"),
    ], min_similarity=0.0)
    matrix = report.matrices["P0001"]
    assert [person.display for person in matrix.people] == ["甲", "乙"]
    assert [person.serial for person in matrix.people] == [1, 2]
    assert matrix.values[0][1] == matrix.values[1][0]


def test_display_falls_back_to_device_tail_when_anonymous():
    """学生不填名字时，设备号后四位是唯一能区分他的东西。"""
    report = sim.analyse([
        {"serial": 1, "device_id": "ABCD2340", "username": "", "problem_id": "P1",
         "language": "cpp", "code": TEMPLATE + LOOP_SUM},
    ])
    assert report.analysed[0].display == "设备 2340"


def test_severity_bands_are_ordered():
    assert sim.severity_band(0.95) == "high"
    assert sim.severity_band(0.80) == "medium"
    assert sim.severity_band(0.60) == "low"


def test_unknown_language_falls_back_to_c_family():
    """认不出来的语言（老数据、外部判题器）按 C 系处理，不报错。"""
    assert sim.language_family("brainfuck") == sim.FAMILY_C
    assert sim.language_family("python") == sim.FAMILY_PYTHON
    assert sim.language_family(None) == sim.FAMILY_C


def test_python_and_cpp_do_not_share_a_family():
    """同一段文本按不同语言解析，注释规则不同 —— 结果不该被当成同一份。

    C 系里 ``#`` 不是注释，所以整行都是代码（行内空白照常压成一个空格）。
    """
    code = "x = 1  # 注释\n"
    assert sim.strip_comments(code, "python") == "x = 1"
    assert sim.strip_comments(code, "cpp") == "x = 1 # 注释"
    assert sim.exact_digest(code, "python") != sim.exact_digest(code, "cpp")


# ======================================================================
# 并排 diff
# ======================================================================


def test_diff_tags_cover_equal_changed_and_missing_side():
    rows = sim.diff_lines("int a = 1;\nint b = 2;\nint c = 3;\n",
                          "int a = 1;\nint b = 9;\n", language="cpp")
    tags = [line.tag for line in rows]
    assert sim.DIFF_EQUAL in tags
    assert sim.DIFF_CHANGED in tags
    assert sim.DIFF_LEFT_ONLY in tags


def test_diff_keeps_indentation_so_it_is_readable():
    """展示用 diff 保留缩进 —— 去掉缩进两段代码摆在一起就读不懂了。"""
    rows = sim.diff_lines("int main() {\n    return 1;\n}",
                          "int main() {\n    return 2;\n}", language="cpp")
    changed = [line for line in rows if line.tag == sim.DIFF_CHANGED]
    assert changed and changed[0].left_text.startswith("    ")


def test_diff_drops_comment_only_lines():
    """注释不参与 diff：抄的时候把注释也抄了，跟"代码像不像"是两件事。"""
    rows = sim.diff_lines("// 说明\nint a = 1;\n", "int a = 1;\n", language="cpp")
    assert all(line.tag == sim.DIFF_EQUAL for line in rows)


def test_diff_context_folds_long_equal_run():
    left = "\n".join(f"int v{i} = {i};" for i in range(20))
    right = "\n".join(f"int v{i} = {i};" for i in range(20))
    rows = sim.diff_lines(left, right, language="cpp", context=2)
    assert any(line.left_text == "…" for line in rows)
    assert len(rows) < 20


def test_diff_line_numbers_start_at_one_and_are_one_sided_when_missing():
    rows = sim.diff_lines("int a = 1;\nint b = 2;\n", "int a = 1;\n", language="cpp")
    assert rows[0].left_no == 1 and rows[0].right_no == 1
    tail = rows[-1]
    assert tail.tag == sim.DIFF_LEFT_ONLY
    assert tail.left_no == 2 and tail.right_no is None and tail.right_text == ""


# ======================================================================
# 分层守卫
# ======================================================================


def test_core_similarity_does_not_import_ui_or_network():
    """``core/`` 的硬约定：不 import PySide6、也不 import ``net/``。

    这条不是洁癖 —— 它是"这一层能脱离界面与网络单测"的全部依据，
    而这个模块的判断结果要能脱离现场复核。
    """
    source = (ROOT / "offline_oj" / "core" / "similarity.py").read_text("utf-8")
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    offenders = [
        name for name in modules
        if name.startswith("PySide6")
        or name == "net" or name.startswith("net.")
        or name.endswith(".net") or ".net." in name
    ]
    assert not offenders, offenders


def test_core_similarity_only_uses_the_standard_library():
    """零新依赖是这个项目的硬约束（打包体积与杀软误报都靠它）。"""
    allowed = {
        "hashlib", "math", "re", "zlib", "collections", "dataclasses",
        "difflib", "typing", "__future__",
    }
    source = (ROOT / "offline_oj" / "core" / "similarity.py").read_text("utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.add((node.module or "").split(".")[0])
    assert modules <= allowed, sorted(modules - allowed)
