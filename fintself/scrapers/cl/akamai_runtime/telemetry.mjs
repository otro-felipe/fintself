import { dirname } from "node:path";
import { createRequire } from "node:module";
import { TextDecoder, TextEncoder } from "node:util";
import { fileURLToPath } from "node:url";

const runtimeDirectory = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

function loadJsdom() {
  try {
    const modulePath = require.resolve("jsdom", {
      paths: [process.cwd(), runtimeDirectory],
    });
    return require(modulePath);
  } catch {
    process.exit(78);
  }
}

async function readRequest() {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
  }
  return JSON.parse(input);
}

async function waitForTelemetry(window) {
  for (let attempt = 0; attempt < 150; attempt += 1) {
    if (typeof window.bmak?.get_telemetry === "function") {
      const telemetry = window.bmak.get_telemetry();
      if (typeof telemetry === "string" && telemetry.length > 0) {
        return telemetry;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("telemetry_unavailable");
}

let dom;
let telemetry = "";
try {
  const { JSDOM, VirtualConsole } = loadJsdom();
  const request = await readRequest();
  if (
    typeof request.html !== "string" ||
    request.frameUrl !==
      "https://mibanco.santander.cl/UI.Web.HB/Private_new/frame/"
  ) {
    process.exit(1);
  }
  dom = new JSDOM(request.html, {
    url: request.frameUrl,
    resources: "usable",
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: new VirtualConsole(),
    beforeParse(window) {
      window.TextEncoder = TextEncoder;
      window.TextDecoder = TextDecoder;
    },
  });
  telemetry = await waitForTelemetry(dom.window);
  process.stdout.write(telemetry);
  telemetry = "";
  dom.window.close();
} catch {
  telemetry = "";
  dom?.window.close();
  process.exit(1);
}
