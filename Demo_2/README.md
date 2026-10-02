# DEMO2：ThreadPool starvation 影響 Kubernetes liveness

以 .NET 6 API 示範：業務 endpoint 同步阻塞 worker，即使 `/healthz` 只回覆 `OK`，也可能無法及時處理 probe。目標是觀察 Runtime 指標、blocked stack、health 延遲，最後看到 **app 容器重啟**（不是 Pod 被重新建立）。

這是人為重現的一種機制，**不是當年線上事件已證實的 RCA**。Thread count 到 100 或 200 不代表 ThreadPool 已達預設上限。

.NET 6 與此處的診斷工具已停止支援，只限隔離的教學環境，不公開到 Internet。

## 組成與取捨

- `/healthz`：立即回覆 `OK`，沒有外部依賴。
- `/bad`：`Thread.Sleep` 同步阻塞 worker，預設 60 秒；不刻意提高 CPU 使用率。
- `/good`：相同延遲，使用可取消的 `Task.Delay`，不佔住 worker 等待。
- `BLOCK_MS`：1–300000 毫秒，預設 60000；無效值使程式啟動失敗。
- app 使用 Runtime 預設 ThreadPool 行為，**不設定最大／最小 worker threads**。
- debug **sidecar** 使用固定版本的 `dotnet-counters`、`dotnet-stack`；共享 process namespace 及 `/tmp` diagnostic socket。這不是 ephemeral container。
- 獨立 k6 Job：256 VUs，持續 3 分鐘；210 秒硬期限，失敗不自動重跑。
- startup probe 保護啟動；liveness 每 5 秒檢查、1 秒 timeout、連續失敗 3 次觸發重啟。
- 刻意不設 readiness probe，避免負載因 Service 摘除 endpoint 而中斷。**不是 production probe 設定建議**。

.NET 6 對部分同步等待 Task 的 API 有較快的 worker 補充機制，因此本例選用 `Thread.Sleep`，不宣稱 `.Result` 一定可以穩定重啟。

## 1. 先準備 images

不需要本機 .NET SDK。GitHub Actions `.github/workflows/build-demo2-images.yml`：

- PR：建置與 smoke test，不發布。
- push 到 `main` 或手動執行：smoke 通過後發布 x64（linux/amd64）images 到 GHCR。
- images：`ghcr.io/beeeeemo/threadpool-demo`、`ghcr.io/beeeeemo/threadpool-demo-debug`。
- tags：`latest`、`sha-<完整 commit SHA>`；現場演示建議兩個 image 都固定到同一個 SHA tag。
- 沿用 `GITHUB_TOKEN`，不需另加 secret。首次發布後請確認兩個 GHCR packages 設為 public；若維持 private，須自行配置 imagePullSecret。

如果 fork 到其他 owner，workflow 會自動使用該 owner 的小寫名稱，但須同步修改 `k8s.yaml` 的兩個 image 路徑。

CI 的 smoke test 驗證 API、無效設定拒絕啟動、跨容器 PID/socket 存取、實際收集 ThreadPool counter samples，以及 `/bad` 執行時 stack 有 `Thread.Sleep`。**它不驗證 Kubernetes probe 重啟；images 僅支援 x64（linux/amd64）**。

## 2. 部署及正常基準

需要 kubectl、可拉取上述 images 的隔離 K8s 叢集。應有約 1 GiB 以上可用記憶體及足夠 CPU，並允許共享 process namespace。所有資源放在專用 `demo2` namespace。

```bash
kubectl apply -f Demo_2/k8s.yaml
kubectl -n demo2 rollout status deployment/threadpool-demo --timeout=120s
kubectl -n demo2 get pods
kubectl -n demo2 logs deployment/threadpool-demo -c app
```

每個 terminal 先設定目標 Pod：

```bash
POD=$(kubectl -n demo2 get pod -l app=threadpool-demo -o jsonpath='{.items[0].metadata.name}')
```

**不要假設 app PID 是 1**；共享 process namespace 後 PID 1 通常是 sandbox process。

在一個 terminal 啟動叢集內的 health observer，先確認持續回覆 `OK`：

```bash
kubectl -n demo2 run health-observer --restart=Never \
  --image=curlimages/curl:8.10.1 --command -- sh -c \
  'while true; do date -u; curl -sS --max-time 2 -w " http=%{http_code} total=%{time_total}s\n" http://threadpool-demo:8080/healthz; sleep 1; done'
kubectl -n demo2 logs -f health-observer
```

Observer 的 2 秒 timeout 與 liveness 的 1 秒 timeout 不同；observer 是額外證據，真正的 probe 結果以 K8s events 為準。

## 3. 先開診斷，再開始壓測

Terminal A，列出 .NET PID：

```bash
kubectl -n demo2 exec "$POD" -c debug -- dotnet-counters ps
```

找到 `ThreadPoolDemo` 那一列，將實際 PID 填入以下命令：

```bash
PID=123  # 替換成剛才列出的 app PID
kubectl -n demo2 exec -it "$POD" -c debug -- \
  dotnet-counters monitor --process-id "$PID" --refresh-interval 1 --counters System.Runtime
```

觀察 .NET 6 的 EventCounters：

- ThreadPool Thread Count (`threadpool-thread-count`)
- ThreadPool Queue Length (`threadpool-queue-length`)
- ThreadPool Completed Work Item Count (`threadpool-completed-items-count`，通常呈現每個更新區間的速率)
- CPU Usage、GC Heap Size 等排除其他壓力來源的指標

先記下沒有負載時的值；不要求特定 thread count，也不把單一 queue 數值當成根因證明。

Terminal B，啟動負載：

```bash
kubectl apply -f Demo_2/load.yaml
kubectl -n demo2 logs -f job/threadpool-load
```

Terminal C，趁 app 尚未重啟時擷取 stack（先設定 `$POD` 與目前 `$PID`）：

```bash
kubectl -n demo2 exec "$POD" -c debug -- \
  dotnet-stack report --process-id "$PID"
```

找 `Thread.Sleep` 與 `Program` 的 endpoint 呼叫鏈。低 CPU、worker 增加／queue 排隊加上多個 blocked stacks，比只看 threads 數量更能支持 starvation 的判斷。

## 4. 觀察 probe 和重啟

另一個 terminal：

```bash
kubectl -n demo2 get pods -w
```

```bash
kubectl -n demo2 get events --sort-by=.metadata.creationTimestamp
kubectl -n demo2 describe pod "$POD"
kubectl -n demo2 get pod "$POD" -o jsonpath='{range .status.containerStatuses[*]}{.name}{": restarts="}{.restartCount}{" lastReason="}{.lastState.terminated.reason}{"\n"}{end}'
```

主要成功條件：

1. 正常基準下 `/healthz` 快速成功。
2. 負載後出現 blocked stacks、Runtime 指標異常或 health 延遲。
3. events 出現 `Liveness probe failed` 和重啟相關紀錄，**app** 的 `restartCount` 增加。
4. 排除 `OOMKilled`；若 metrics-server 可用，搭配 `kubectl -n demo2 top pod --containers` 觀察 CPU／memory，但低頻取樣不能單獨排除短暫 throttling。

app 重啟後 PID 和 socket 會變，counters 連線會結束；重新執行 `dotnet-counters ps` 並 attach 新 PID。Debug sidecar 通常不會一起重啟。重啟過快時，可重新部署後先只做一次短壓測並提早抓 stack。

## 5. 停止、對照及恢復

立即停止負載：

```bash
kubectl -n demo2 delete job threadpool-load --ignore-not-found
```

**停止客戶端不會取消已經進入 `Thread.Sleep` 的請求**，所以最多還需等待 `BLOCK_MS` 的時間，或重啟 deployment：

```bash
kubectl -n demo2 rollout restart deployment/threadpool-demo
kubectl -n demo2 rollout status deployment/threadpool-demo --timeout=120s
```

要比較非阻塞路徑：停止原 Job，將 `Demo_2/load.yaml` 中 `ENDPOINT` 的 `value: bad` 改為 `value: good`，再 apply。同樣 256 VUs／60 秒等待，觀察 workers、stack 與 health 的差異。重跑 `/bad` 時改回 `bad`。Job 完成後若要再次執行，也要先刪掉舊 Job 再 apply。

如果沒有觸發重啟：

- 先確認負載真的有進入 `/bad`，沒有 image pull、DNS 或 load Pod OOM 問題。
- 以 blocked stacks、Runtime 指標或 health 延遲作為基本成果，不把未出現的重啟說成已重現。
- 可以在 `load.yaml` 提高 VUs，或提高 `k8s.yaml` 的 `BLOCK_MS`（不超過 300000），一次只改一個因素；重新建立 Job／rollout 後比較。
- 有限資源下不要無限制增加併發。若看到 OOM 或 CPU 飽和，先停止，該次不能當作純 starvation 示範。
- 若只看到 thread count 增加，queue／health 沒有惡化，可能是 Runtime 已補足 workers，不等於 starvation 已成立。

## 6. 清理

確認 `demo2` namespace 僅包含此 demo，再執行：

```bash
kubectl delete namespace demo2
```

## 本機有 Docker 時的驗證

```bash
docker build -t demo2-app:smoke -f Demo_2/Dockerfile Demo_2
docker build -t demo2-debug:smoke -f Demo_2/Dockerfile.debug Demo_2
python3 Demo_2/smoke.py
```

腳本使用隨機名稱的容器與 volume，結束時清理自己建立的資源，不碰既有容器。

## 參考

- [Microsoft：Debug ThreadPool starvation](https://learn.microsoft.com/dotnet/core/diagnostics/debug-threadpool-starvation)
- [Microsoft：Collect diagnostics in containers](https://learn.microsoft.com/dotnet/core/diagnostics/diagnostics-in-containers)
