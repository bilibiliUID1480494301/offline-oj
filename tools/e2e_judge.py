import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve()))
os.environ['OFFLINE_OJ_HOME'] = tempfile.mkdtemp(prefix='oj_e2e_')
from offline_oj.context import AppContext
from offline_oj.paths import build_paths
from offline_oj.core.compilers import CompilerDetector
from offline_oj.core.models import Language, Problem, TestCase
from offline_oj.core.judge import Judge
from offline_oj.core.runners import make_runner, self_test

ctx = AppContext.create(build_paths().ensure_layout())
for key in ("cpp", "c", "python", "javac", "java"):
    info = CompilerDetector.detect(key)
    if info:
        ctx.settings.set_compiler_path(key, info.path)
ctx.settings.save()
paths = ctx.settings.compiler_paths()
print("检测到的工具链:", {k: (v or "-") for k, v in paths.items()})

prob = Problem(id='SUM', title='A+B', description='x', time_limit=5000, memory_limit=256,
               testcases=[TestCase(input='1 2\n', output='3\n'), TestCase(input='10 20\n', output='30\n')])

print('--- 工具链自检（真实编译+运行） ---')
for lang in (Language.PYTHON, Language.JAVA, Language.CPP, Language.C):
    if not all(paths.get(k) for k in lang.key_paths):
        print(f'{lang.value}: 跳过（未配置）')
        continue
    ok, msg = self_test(lang, paths, ctx.paths.work_dir)
    print(f'{lang.value} self_test: {"OK" if ok else "FAIL"} - {msg}')

solutions = {
    Language.PYTHON: "a,b=map(int,input().split())\nprint(a+b)\n",
    Language.JAVA: "import java.util.*;\npublic class Solution{public static void main(String[] a){Scanner s=new Scanner(System.in);System.out.println(s.nextInt()+s.nextInt());}}\n",
    Language.CPP: "#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b<<std::endl;}\n",
}
wrong = {Language.PYTHON: "print(0)\n"}
print('--- 端到端判题 ---')
for lang, code in solutions.items():
    if not all(paths.get(k) for k in lang.key_paths):
        continue
    runner = make_runner(lang, paths, ctx.paths.work_dir, optimize=True)
    r = Judge(runner).judge(prob, code)
    print(f'{lang.value} 正确解: {r.verdict.value} {r.passed}/{r.total} {r.max_time_ms:.0f}ms {r.max_memory_mb:.1f}MB')
    if not r.accepted:
        print(r.text_report())
for lang, code in wrong.items():
    runner = make_runner(lang, paths, ctx.paths.work_dir, optimize=True)
    r = Judge(runner).judge(prob, code)
    print(f'{lang.value} 错误解: {r.verdict.value} {r.passed}/{r.total}')
