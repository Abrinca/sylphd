// worker_threads shim for a Web Worker module: `self` forwards to parentPort.
import { parentPort, workerData } from "node:worker_threads";

globalThis.self = {
  postMessage(data, transfer) {
    parentPort.postMessage(data, transfer);
  },
  set onmessage(fn) {
    parentPort.removeAllListeners("message");
    parentPort.on("message", (data) => fn({ data }));
  },
};

await import(workerData.url);
