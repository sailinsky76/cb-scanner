#!/usr/bin/env bash
# 一键离线 DoD（HANDOFF §8）：七项全绿才算改完。不联网，只写 reports/。
#
#   bash check.sh              # 全跑
#   PY="py -3.11" bash check.sh   # 指定解释器（Windows 上用这个）
#
# 耗时提醒：**断网环境下 selftest 要跑 2 分钟以上**，回放各 15-20 秒 ——
# 慢的不是断言，是源层在非 mock 路径上试着拉交易日历、每次都要等连接超时。
# 能联网的机器上快很多。别因为它慢就跳过：这七项是唯一拦得住行为退化的东西。
set -u
PY="${PY:-python3}"
export PYTHONIOENCODING=utf-8        # Windows GBK 终端吃不下 ✔✘🔴 等字符
cd "$(dirname "$0")" || exit 2

pass=0; fail=0; selftest_out=""
run() {   # run "标题" 命令...
  local title="$1"; shift
  printf '── %-44s' "$title"
  local out
  if out=$("$@" 2>&1); then
    printf '\033[32m绿\033[0m\n'; pass=$((pass+1))
  else
    printf '\033[31m红\033[0m\n'; fail=$((fail+1))
    printf '%s\n' "$out" | tail -20 | sed 's/^/     │ /'
  fi
  LAST_OUT="$out"
}

echo "cb_scanner 离线自检（$PY）—— 断网下约 3 分钟"
run "selftest_fixes（取数健壮性+措辞回归）" $PY selftest_fixes.py
selftest_out="$LAST_OUT"
run "tests_utils（工具函数）"               $PY tests_utils.py
run "tests_sources（源层）"                 $PY tests_sources.py
run "replay_20260808（08-08 实盘复现）"     $PY replay_20260808.py
run "replay_20260809（08-09 实盘复现）"     $PY replay_20260809.py
run "run.py --mock（全流程+5 条不变量）"    $PY run.py --mock --format console markdown html
# 第 7 项排在最后、也只能排最后：前六项管的是「报告对不对」，这一项管的是
# 「对的报告有没有送到手机上」。notify.py 是**配上才会坏**的那一类（见其文件头 §②
# 那个飞书 markdown 的 bug：只配 SC_SENDKEY 时它永远不暴露），没有断言守着，
# 你会在最不想调试的那天调试它。不联网、约 1 秒。
run "notify.py --selftest（推送渠道转换）"  $PY notify.py --selftest

echo
if [ "$fail" -eq 0 ]; then
  n=$(printf '%s\n' "$selftest_out" | grep -c '^  \[PASS\]')
  printf '\033[32m全绿\033[0m：%d/%d 项通过（selftest 断言 %d 条）\n' "$pass" "$pass" "$n"
  echo "接着照 STATE.md 的「当前任务」开工。"
  exit 0
else
  printf '\033[31m有红\033[0m：%d 项失败，%d 项通过 —— 先修红的，别往上叠改动。\n' "$fail" "$pass"
  exit 1
fi
