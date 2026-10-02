# DEMO2：ThreadPool starvation 影響 Kubernetes liveness

以 .NET 6 API 示範：業務 endpoint 同步阻塞 worker，即使 `/healthz` 只回覆 `OK`，也可能無法及時處理 probe。正常部署 **只有 API 容器，沒有診斷工具、debug sidecar、共享 PID namespace 或預設診斷 volume**。出問題後才透過 `kubectl debug` 加入 ephemeral container 排查。

這是人為重現的一種機制，**不是當年線上事件已證實的 RCA**。Thread count 到 100 或 200 不代表 ThreadPool 已達預設上限。Liveness 重啟的是 app 容器，不是重新建立 Pod。

## 組成

- `/healthz`：立即回覆 `OK`，沒有外部依賴。
- `/bad`：`Thread.Sleep` 同步阻塞 worker，預設 60 秒，非 CPU 密集工作。
- `/good`：相同延遲，使用可取消的 `Task.Delay`，不佔住 worker 等待。
- `BLOCK_MS`：1–300000 毫秒，預設 60000；無效值使程式啟動失敗。
- ThreadPool 使用 Runtime 預設行為，**不設定最大／最小 worker threads**。
- 獨立 k6 Job：256 VUs、3 分鐘，210 秒硬期限，不自動重跑。
- startup probe 保護啟動；liveness 每 5 秒檢查、1 秒 timeout、連續失敗 3 次觸發重啟。
- 刻意不設 readiness probe，避免 Service 摘除 endpoint 中斷負載；不是 production probe 建議。

.NET 6 對部分同步等待 Task 的 API 有較快的 worker 補充機制，因此選用 `Thread.Sleep`，不宣稱 `.Result` 一定可以穩定重啟。

## 1. 部署及正常基準

使用隔離的 x64 K8s 叢集、kubectl，並預留 CPU／記憶體。Ephemeral containers、`--target` PID namespace targeting 須受叢集 Runtime 支援；使用支援 `--profile=general` 的 kubectl。操作身分需要更新 `pods/ephemeralcontainers` 的權限。叢集安全政策也必須允許 debug profile 的 `SYS_PTRACE` capability；不允許時不要直接放寬整個叢集政策。

```bash
kubectl apply -f Demo_2/k8s.yaml
kubectl -n demo2 rollout status deployment/threadpool-demo --timeout=120s
POD=$(kubectl -n demo2 get pod -l app=threadpool-demo -o jsonpath='{.items[0].metadata.name}')
kubectl -n demo2 logs "$POD" -c app
```

此時 Pod 應只有 `app` 一個容器。啟動 health observer，先確認持續回覆 `OK`：

```bash
kubectl -n demo2 run health-observer --restart=Never \
  --image=curlimages/curl:8.10.1 --command -- sh -c \
  'while true; do date -u; curl -sS --max-time 2 -w " http=%{http_code} total=%{time_total}s\n" http://threadpool-demo:8080/healthz; sleep 1; done'
kubectl -n demo2 logs -f health-observer
```

Observer 的 2 秒 timeout 與 liveness 的 1 秒 timeout 不同；真正的 probe 結果以 K8s events 為準。重跑 observer 前先刪除舊的 `health-observer` Pod。

## 2. 製造問題，再加入 debug container

另一個 terminal 啟動負載：

```bash
kubectl apply -f Demo_2/load.yaml
kubectl -n demo2 logs -f job/threadpool-load
```

排查 terminal 重新設定 `$POD`，加入臨時診斷容器：

```bash
POD=$(kubectl -n demo2 get pod -l app=threadpool-demo -o jsonpath='{.items[0].metadata.name}')
kubectl -n demo2 debug -it "pod/$POD" \
  --target=app --container="debug-$(date +%s)" --profile=general \
  --image=ghcr.io/beeeeemo/threadpool-demo-debug:latest -- bash
```

**以下命令在 debug container 的 shell 裡執行。** 先從 app 啟動 log 確認 PID；本例沒有共享 process namespace，通常為 1，但不要盲目假設。驗證看到的是目標程序：

```bash
PID=1  # 替換成 app 啟動 log 顯示的 PID
tr '\0' ' ' < "/proc/$PID/cmdline"
echo
```

應看到 `dotnet ThreadPoolDemo.dll`。若不是，`--target` 可能未受 Runtime 支援；先停止，不要對錯誤程序 attach。

PID 看得到不代表 filesystem 相同。診斷工具仍須存取 app 的 Unix diagnostic socket；本例 app 未自訂 `TMPDIR`，所以透過 `/proc/<PID>/root` 存取 app 的 `/tmp`，**不修改 app 的 filesystem 配置**：

```bash
export TMPDIR="/proc/$PID/root/tmp"
find "$TMPDIR" -maxdepth 1 -name "dotnet-diagnostic-${PID}-*-socket"
dotnet-counters ps
dotnet-counters monitor --process-id "$PID" --refresh-interval 1 --counters System.Runtime
```

如果 socket 找不到，確認 PID、app 是否已重啟、process namespace targeting 及讀取 `/proc/<PID>/root` 的權限。此路徑是針對本例 Linux／root app；非 root、關閉 diagnostics 或自訂 `TMPDIR` 的正式服務需另外確認，不保證直接套用。

觀察 ThreadPool Thread Count、ThreadPool Queue Length、ThreadPool Completed Work Item Count（通常呈現更新區間速率）、CPU Usage。不要只憑 thread count 判定 starvation。

按 Ctrl+C 停止 counters 後，在同一 shell 抓 stack：

```bash
dotnet-stack report --process-id "$PID"
```

找 `Thread.Sleep` 與 `Program` endpoint 的呼叫鏈。要同時看 counters 與 stack，可再加入另一個唯一名稱的 debug container，重新設定 PID／TMPDIR。

**app 重啟後，舊 ephemeral container 可能仍停留在舊 PID namespace，不能只改 PID 就重新 attach。** 重新執行 `kubectl debug --target=app` 加入新容器，再確認程序及 socket。Ephemeral container 無法從現有 Pod 刪除；退出 shell 使其結束，最終刪除／重新建立 Pod 才清掉紀錄。

## 3. 確認重啟與排除其他原因

在 host terminal：

```bash
kubectl -n demo2 get pods -w
```

```bash
kubectl -n demo2 get events --sort-by=.metadata.creationTimestamp
kubectl -n demo2 describe pod "$POD"
kubectl -n demo2 get pod "$POD" -o jsonpath='{range .status.containerStatuses[*]}{.name}{": restarts="}{.restartCount}{" lastReason="}{.lastState.terminated.reason}{"\n"}{end}'
```

主要成功條件：正常 health 快速成功；壓測後有 blocked stacks、Runtime 指標異常或 health 延遲；events 有 liveness failure／restart，app `restartCount` 增加。排除 `OOMKilled`；若有 metrics-server，搭配 `kubectl -n demo2 top pod --containers`，但低頻 CPU 取樣不能單獨排除 throttling。

如果重啟太快來不及診斷，可提高 `k8s.yaml` 的 liveness `failureThreshold` 後重新 apply，延長取證窗口；記錄這是演示調整，完成後恢復為 3。

## 4. 停止、對照及恢復

```bash
kubectl -n demo2 delete job threadpool-load --ignore-not-found
```

停止客戶端不會取消已進入 `Thread.Sleep` 的請求。等待 `BLOCK_MS`，或重新建立 app Pod（同時清掉舊 ephemeral containers）：

```bash
kubectl -n demo2 rollout restart deployment/threadpool-demo
kubectl -n demo2 rollout status deployment/threadpool-demo --timeout=120s
```

非阻塞對照：停止原 Job，將 `load.yaml` 的 `ENDPOINT` 從 `bad` 改成 `good` 再 apply；觀察相同併發／延遲下的 health 與 workers。重跑任一負載前先刪除舊 Job。

若沒有重啟，先確認負載確實進入 `/bad`，沒有 DNS、image pull 或 load Pod OOM 問題。基本成果是 blocked stacks、Runtime 指標或 health 延遲，不宣稱尚未重現的 restart。可逐次調高 VUs 或 `BLOCK_MS`，一次改一個因素；若 OOM 或 CPU 飽和，先停止，不能當作純 starvation 證據。只有 threads 增加但 queue／health 沒惡化，可能只是 Runtime 已補足 workers。

## 5. 清理

確認專用 `demo2` namespace 沒有其他資源後清理：

```bash
kubectl delete namespace demo2
```

## 參考

- [Microsoft：Debug ThreadPool starvation](https://learn.microsoft.com/dotnet/core/diagnostics/debug-threadpool-starvation)
- [Microsoft：Collect diagnostics in containers](https://learn.microsoft.com/dotnet/core/diagnostics/diagnostics-in-containers)
- [Kubernetes：Debug Running Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/)
