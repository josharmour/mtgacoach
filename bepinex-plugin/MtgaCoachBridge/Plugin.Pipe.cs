using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using BepInEx;
using BepInEx.Logging;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;
using GreClient.Rules;
using GreClient.CardData;
using Wotc.Mtgo.Gre.External.Messaging;
using HarmonyLib;

namespace MtgaCoachBridge
{
    public partial class Plugin
    {
        private JObject DispatchCommandToUnityThread(PipeCommand cmd, int timeoutMs)
        {
            var host = MtgaCoachHost.Instance;
            if (host == null)
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = "MtgaCoachHost not available"
                });
                return cmd.WaitForResponse(timeoutMs);
            }

            if (Thread.CurrentThread.ManagedThreadId == host.MainThreadId)
            {
                ExecutePipeCommand(cmd);
                return cmd.WaitForResponse(timeoutMs);
            }

            var unityContext = host.UnityContext;
            if (unityContext != null)
            {
                // Primary path: Post via captured Unity SyncContext.
                // Fallback queue is processed by the host's Update() if Post
                // ever fails to deliver.
                unityContext.Post(_ => ExecutePipeCommand(cmd), null);
            }
            else
            {
                host.PendingCommands.Enqueue(cmd);
            }

            return cmd.WaitForResponse(timeoutMs);
        }

        // -------------------------------------------------------------------
        // Named pipe CLIENT â€” connects to Python-owned server pipe.
        // Reversed architecture: Python creates the pipe, plugin connects.
        // This avoids MTGA internals grabbing the pipe and scene transitions
        // killing the server.
        // -------------------------------------------------------------------

        private void PipeClientLoop()
        {
            // Reconnect loop with adaptive backoff.
            //   - Short connect timeout (1s) so we poll frequently when the
            //     Python server is momentarily down (scene transitions,
            //     Python process restart).
            //   - Successful connections reset the retry delay.
            //   - Failed reconnects back off from 200ms up to 2s so we don't
            //     spin on the CPU when Python is truly gone.
            int retryMs = 200;
            const int minRetryMs = 200;
            const int maxRetryMs = 2000;
            int consecutiveTimeouts = 0;

            while (true)
            {
                TcpClient client = null;
                bool connectedThisIteration = false;
                try
                {
                    client = new TcpClient();
                    var result = client.BeginConnect("127.0.0.1", 44222, null, null);
                    bool success = result.AsyncWaitHandle.WaitOne(1000); // 1s timeout â€” retry if Python isn't up yet
                    if (!success)
                    {
                        throw new TimeoutException("TCP connection to Python server timed out");
                    }
                    client.EndConnect(result);

                    // Guard against TCP self-connect: on loopback, if the OS
                    // hands our client socket source port 44222 (same as the
                    // dest), a TCP simultaneous-open connects us to ourselves.
                    // The socket reports ESTABLISHED but there is no Python
                    // server on the other end, so we'd hang forever. Detect it
                    // (local port == remote port) and retry with a fresh socket.
                    var localEp = client.Client.LocalEndPoint as System.Net.IPEndPoint;
                    if (localEp != null && localEp.Port == 44222)
                    {
                        _log.LogWarning("TCP self-connect detected (local port == 44222); retrying");
                        try { client.Close(); } catch { }
                        System.Threading.Thread.Sleep(50);
                        continue;
                    }

                    connectedThisIteration = true;
                    consecutiveTimeouts = 0;
                    retryMs = minRetryMs;
                    _log.LogInfo("TCP client connected to Python server on port 44222");

                    HandleClient(client);
                    _log.LogInfo("TCP client lost connection (HandleClient returned), reconnecting...");
                }
                catch (TimeoutException)
                {
                    // Python server not up yet â€” usually means the Python
                    // process is restarting or between server recreations.
                    consecutiveTimeouts++;
                    if (consecutiveTimeouts == 1 || consecutiveTimeouts % 10 == 0)
                    {
                        _log.LogInfo(
                            $"TCP client: Python server not available " +
                            $"(timeout {consecutiveTimeouts}), retrying in {retryMs}ms"
                        );
                    }
                    // Back off on repeated timeouts so we don't spin.
                    retryMs = System.Math.Min(maxRetryMs, retryMs * 2);
                }
                catch (System.Net.Sockets.SocketException sex)
                {
                    // Connection refused = Python server not running. Same
                    // situation as a connect timeout, so same treatment:
                    // back off and log every 10th attempt. (Previously this
                    // reset the retry to 200ms and logged EVERY attempt â€”
                    // ~5 log lines/sec for as long as Python was down.)
                    consecutiveTimeouts++;
                    if (consecutiveTimeouts == 1 || consecutiveTimeouts % 10 == 0)
                    {
                        _log.LogInfo(
                            $"TCP client: Python server not available " +
                            $"({sex.SocketErrorCode}, attempt {consecutiveTimeouts}), " +
                            $"retrying in {retryMs}ms"
                        );
                    }
                    retryMs = System.Math.Min(maxRetryMs, retryMs * 2);
                }
                catch (Exception ex)
                {
                    _log.LogWarning($"TCP client error: {ex.GetType().Name}: {ex.Message}");
                    // Reset retry after non-timeout errors â€” they're usually
                    // transient.
                    retryMs = minRetryMs;
                }
                finally
                {
                    try { client?.Close(); } catch { }
                }

                // If we DID connect successfully and HandleClient returned,
                // reconnect aggressively â€” the Python server should be ready
                // to accept us again.
                int sleepMs = connectedThisIteration ? minRetryMs : retryMs;
                Thread.Sleep(sleepMs);
            }
        }

        private void HandleClient(TcpClient client)
        {
            using var stream = client.GetStream();
            using var reader = new StreamReader(stream, Encoding.UTF8, false, 4096, leaveOpen: true);
            using var writer = new StreamWriter(stream, new UTF8Encoding(false), 4096, leaveOpen: true)
            {
                AutoFlush = true
            };

            while (client.Connected)
            {
                string line;
                try
                {
                    line = reader.ReadLine();
                }
                catch
                {
                    break;
                }

                if (line == null)
                    break;

                line = line.Trim();
                if (string.IsNullOrEmpty(line))
                    continue;

                try
                {
                    var json = JObject.Parse(line);
                    string action = json.Value<string>("action") ?? "";

                    // Handle ping directly on TCP thread â€” doesn't need Unity main thread.
                    // This is critical because Update() stops after OnDestroy (scene transitions).
                    if (action == "ping")
                    {
                        var pingResp = new JObject
                        {
                            ["ok"] = true,
                            ["version"] = PluginInfo.Version
                        };
                        writer.WriteLine(pingResp.ToString(Formatting.None));
                        continue;
                    }

                    // All other commands need Unity main thread (GameManager access)
                    var cmd = new PipeCommand(json);
                    var response = DispatchCommandToUnityThread(cmd, UnityCommandTimeoutMs);
                    writer.WriteLine(response.ToString(Formatting.None));
                }
                catch (Exception ex)
                {
                    var errorResp = new JObject
                    {
                        ["ok"] = false,
                        ["error"] = $"Parse error: {ex.Message}"
                    };
                    try { writer.WriteLine(errorResp.ToString(Formatting.None)); } catch { break; }
                }
            }

            _log.LogInfo("TCP client disconnected");
            writer.Dispose();
        }

        // -------------------------------------------------------------------
        // GameManager access (cached, main thread only)
        // -------------------------------------------------------------------

        private GameManager GetGameManager()
        {
            // Host's Update() refreshes this every second. The host lives on
            // its own DDOL'd GameObject so it survives MTGA scene transitions
            // even when this Plugin's MonoBehaviour is destroyed.
            var host = MtgaCoachHost.Instance;
            if (host != null)
            {
                var cached = host.GetGameManager();
                if (cached != null)
                    return cached;
            }
            // Fallback: direct lookup (e.g. before host's first Update tick).
            return FindObjectOfType<GameManager>();
        }

        // -------------------------------------------------------------------
        // Command processing (runs on Unity main thread)
        // -------------------------------------------------------------------


    }

    // -------------------------------------------------------------------
    // Helper: pipe command with synchronous response channel
    // -------------------------------------------------------------------

    internal class PipeCommand
    {
        public JObject Json { get; }
        private JObject _response;
        private readonly ManualResetEventSlim _signal = new ManualResetEventSlim(false);

        public PipeCommand(JObject json)
        {
            Json = json;
        }

        public void SetResponse(JObject response)
        {
            _response = response;
            _signal.Set();
        }

        public JObject WaitForResponse(int timeoutMs)
        {
            if (_signal.Wait(timeoutMs))
                return _response;

            return new JObject
            {
                ["ok"] = false,
                ["error"] = "Command timed out waiting for Unity main thread"
            };
        }
    }

}
