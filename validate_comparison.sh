#!/usr/bin/env bash
# Standalone validation script: compare 3 methods of measuring CPU per cgroup slice
# Run this directly on the AHV host (ssh root@<host_ip>)
# Usage: bash validate_comparison.sh [interval_seconds]

INTERVAL=${1:-30}

SLICES=("/sys/fs/cgroup/ahv.slice/ahv-cvm.slice" "/sys/fs/cgroup/ahv.slice/ahv-uvms.slice" "/sys/fs/cgroup/system.slice")
NAMES=("ahv-cvm.slice" "ahv-uvms.slice" "system.slice")

get_usage_usec() {
  local f="$1/cpu.stat"
  if [[ -r "$f" ]]; then
    grep "^usage_usec" "$f" | awk '{print $2}'
  else
    echo "0"
  fi
}

get_all_pids_under() {
  local dir="$1"
  local f="$dir/cgroup.procs"
  [[ -r "$f" ]] || return
  local pid sub
  while read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] && echo "$pid"
  done < "$f" 2>/dev/null
  for sub in "$dir"/*/; do
    [[ -d "$sub" ]] && get_all_pids_under "$sub"
  done
}

get_all_tids_under() {
  local dir="$1"
  local pid tid
  get_all_pids_under "$dir" | sort -u | while read -r pid; do
    [[ -z "$pid" ]] && continue
    for tid in /proc/$pid/task/*; do
      [[ -d "$tid" ]] && echo "${tid##*/}"
    done 2>/dev/null
  done | sort -u
}

collect_schedstat() {
  local dir="$1"
  local total_run=0
  local total_wait=0
  while read -r tid; do
    [[ -z "$tid" ]] && continue
    local f="/proc/$tid/schedstat"
    [[ -r "$f" ]] || continue
    read -r run wait _ < "$f" 2>/dev/null || continue
    total_run=$((total_run + run))
    total_wait=$((total_wait + wait))
  done < <(get_all_tids_under "$dir")
  echo "$total_run $total_wait"
}

echo "=== 3-Way CPU Validation ==="
echo "Interval: ${INTERVAL}s"
echo ""

# --- Snapshot 1 ---
echo "Taking snapshot 1..."
for i in 0 1 2; do
  eval "cpustat_prev_$i=$(get_usage_usec ${SLICES[$i]})"
  read run wait <<< $(collect_schedstat ${SLICES[$i]})
  eval "sched_run_prev_$i=$run"
  eval "sched_wait_prev_$i=$wait"
done

echo "Waiting ${INTERVAL} seconds..."
sleep $INTERVAL

# --- Snapshot 2 ---
echo "Taking snapshot 2..."
for i in 0 1 2; do
  eval "cpustat_curr_$i=$(get_usage_usec ${SLICES[$i]})"
  read run wait <<< $(collect_schedstat ${SLICES[$i]})
  eval "sched_run_curr_$i=$run"
  eval "sched_wait_curr_$i=$wait"
done

# --- systemd-cgtop (separate measurement) ---
echo "Running systemd-cgtop..."
CGTOP_OUT=$(systemd-cgtop -b -n 2 -d 1 2>/dev/null)

echo ""
echo "============================================================"
printf "%-20s %12s %12s %12s\n" "Slice" "schedstat" "cpu.stat" "cgtop"
printf "%-20s %12s %12s %12s\n" "" "(x_cores)" "(x_cores)" "(x_cores)"
echo "------------------------------------------------------------"

for i in 0 1 2; do
  name="${NAMES[$i]}"

  # Method 1: schedstat x_cores
  eval "run_prev=\$sched_run_prev_$i"
  eval "run_curr=\$sched_run_curr_$i"
  delta_run=$((run_curr - run_prev))
  interval_ns=$((INTERVAL * 1000000000))
  sched_xcores=$(awk "BEGIN {printf \"%.2f\", $delta_run / $interval_ns}")

  # Method 2: cpu.stat x_cores
  eval "cpustat_prev=\$cpustat_prev_$i"
  eval "cpustat_curr=\$cpustat_curr_$i"
  delta_usec=$((cpustat_curr - cpustat_prev))
  interval_usec=$((INTERVAL * 1000000))
  cpustat_xcores=$(awk "BEGIN {printf \"%.2f\", $delta_usec / $interval_usec}")

  # Method 3: cgtop CPU% -> x_cores
  # Find the matching line from the second iteration of cgtop
  if [[ "$name" == "system.slice" ]]; then
    cgtop_pattern="^system.slice "
  elif [[ "$name" == "ahv-cvm.slice" ]]; then
    cgtop_pattern="ahv.slice/ahv-cvm.slice "
  elif [[ "$name" == "ahv-uvms.slice" ]]; then
    cgtop_pattern="ahv.slice/ahv-uvms.slice "
  fi

  # Get the LAST match (from the second iteration)
  cgtop_cpu=$(echo "$CGTOP_OUT" | grep "$cgtop_pattern" | tail -1 | awk '{print $3}')
  if [[ -z "$cgtop_cpu" || "$cgtop_cpu" == "-" ]]; then
    cgtop_xcores="-"
  else
    cgtop_xcores=$(awk "BEGIN {printf \"%.2f\", $cgtop_cpu / 100}")
  fi

  printf "%-20s %12s %12s %12s\n" "$name" "$sched_xcores" "$cpustat_xcores" "$cgtop_xcores"
done

echo "============================================================"
echo ""
echo "If all 3 columns match closely, our measurement is correct."
echo ""
