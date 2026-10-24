import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { test } from "node:test";
import { chromium } from "playwright-core";

const root = path.resolve(import.meta.dirname, "..");
const payload = (config) =>
  JSON.parse(
    execFileSync("uv", ["run", "python", "scripts/browser_payload.py"], {
      cwd: root,
      input: JSON.stringify(config),
      maxBuffer: 20 * 1024 * 1024,
      encoding: "utf8",
    }),
  );

test("real browser worker shares files, transfers retained deltas and preserves warm scratch", {
  timeout: 120000,
}, async () => {
  const worker = `import {loadPyodide} from '/pyodide/pyodide.mjs';
const runtime = await loadPyodide({indexURL:'/pyodide/'});
self.onmessage=async e=>{try{self.postMessage({result:await runtime.runPythonAsync(e.data)});}catch(e){self.postMessage({error:String(e)});}};
self.postMessage({ready:true, absent:typeof globalThis.pyodide==='undefined'});`;
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://localhost");
      if (url.pathname === "/") {
        response.end("<html>synthetic browser test</html>");
        return;
      }
      if (url.pathname === "/worker.mjs") {
        response.setHeader("Content-Type", "text/javascript");
        response.end(worker);
        return;
      }
      if (!url.pathname.startsWith("/pyodide/") || url.pathname.includes("..")) throw Error("path");
      const file = path.join(root, "node_modules/pyodide", url.pathname.slice(9));
      response.setHeader(
        "Content-Type",
        file.endsWith(".wasm") ? "application/wasm" : "text/javascript",
      );
      response.end(await readFile(file));
    } catch {
      response.statusCode = 404;
      response.end();
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
      headless: true,
      args: process.env.CI ? ["--no-sandbox"] : [],
    });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const start = () =>
      page.evaluate(
        () =>
          new Promise((resolve) => {
            globalThis.testWorker?.terminate();
            globalThis.testWorker = new Worker("/worker.mjs", { type: "module" });
            globalThis.testWorker.onmessage = (e) => resolve(e.data);
          }),
      );
    assert.deepEqual(await start(), { ready: true, absent: true });
    const run = async (config) => {
      const generated = payload(config);
      const reply = await page.evaluate(
        (code) =>
          new Promise((resolve) => {
            globalThis.testWorker.onmessage = (e) => resolve(e.data);
            globalThis.testWorker.postMessage(code);
          }),
        generated.code,
      );
      assert.equal(reply.error, undefined, reply.error);
      const value = JSON.parse(reply.result);
      assert.equal(value.nonce, generated.nonce);
      return value;
    };
    let head = "";
    let baseline = [];
    const retained = new Map();
    let shellReady = false;
    const execute = async (code, language = "python", extra = {}) => {
      const operation_id = randomUUID();
      const prepared = await run({
        operation_id,
        phase: "prepare",
        key: "synthetic",
        head,
        restore: [],
      });
      const config = {
        operation_id,
        key: "synthetic",
        head,
        phase: "execute",
        warm: prepared.warm,
        code,
        language,
        cwd: "work",
        restore: [],
        baseline,
        legacy_paths: [],
        inputs: [],
        blocked_modules: [],
        ...extra,
      };
      if (!prepared.warm) {
        for (const [path, data] of retained) {
          const restored = await run({
            ...config,
            phase: "restore",
            continuation: config.continuation || false,
            restore: [{ path, data }],
          });
          assert.equal(restored.outcome || "ok", "ok");
          config.continuation = true;
        }
      }
      if (language === "bash" && !shellReady) config.include_shell = true;
      const value = await run(config);
      if (value.manifest) {
        for (const item of value.files) retained.set(item.path, item.data);
        for (const key of retained.keys())
          if (!value.manifest.some((item) => item.path === key)) retained.delete(key);
        head = operation_id;
        baseline = value.manifest;
      }
      if (language === "bash") shellReady = true;
      return value;
    };
    const first = await execute(
      'from pathlib import Path\nPath(\'scratch.bin\').write_bytes(bytes(256*1024*1024))\nPath(\'../scripts/data.json\').write_text(\'{"items":[{"name":"α","n":2},{"name":"β","n":3}]}\',encoding=\'utf-8\')\nprint(\'python ready\')',
    );
    assert.equal(first.outcome, "ok", first.stderr);
    assert(first.manifest.every((item) => !item.path.startsWith("work/")));
    assert(first.files.reduce((sum, file) => sum + file.data.length, 0) < 20000);
    const shell = await execute(
      "jq -r '.items[] | select(.n>2) | .name' scripts/data.json | sed 's/β/BETA/' > outputs/result.txt; stat -c %s scratch.bin; find ../scripts -type f | rg data.json; awk 'END {print NR}' outputs/result.txt",
      "bash",
    );
    assert.equal(shell.outcome, "ok", shell.stderr);
    assert.match(shell.stdout, /268435456/);
    assert.match(shell.stdout, /data.json/);
    assert.equal(
      shell.files.some((item) => item.path === "scripts/data.json"),
      false,
    );
    const cross = await execute(
      "from pathlib import Path\nassert Path('scratch.bin').stat().st_size==256*1024*1024\nassert Path('outputs/result.txt').read_text()=='BETA\\n'\nprint('shared')",
    );
    assert.equal(cross.outcome, "ok", cross.stderr);
    assert.equal(
      cross.files.some((item) => item.path === "outputs/result.txt"),
      false,
    );
    const binary = await execute(
      "from pathlib import Path\nPath('binary').write_bytes(bytes(range(256))*131072)",
    );
    assert.equal(binary.outcome, "ok", binary.stderr);
    const copied = await execute(
      "wc -c binary; cp binary copied; mv copied renamed; rm -f missing; rg --files /scripts",
      "bash",
    );
    assert.equal(copied.outcome, "ok", copied.stderr);
    assert.match(copied.stdout, /33554432/);
    const verified = await execute(
      "from pathlib import Path\nassert Path('renamed').read_bytes()==bytes(range(256))*131072",
    );
    assert.equal(verified.outcome, "ok", verified.stderr);
    const source =
      "from pathlib import Path\nPath('scripts/.runs/latest/source.py').write_text('forged')\nprint('x'*20000)";
    const provenance = await execute(source);
    assert.equal(provenance.outcome, "ok", provenance.stderr);
    assert.equal(provenance.stdout_truncated, true);
    assert.equal(
      Buffer.from(
        provenance.files.find((item) => item.path === "scripts/.runs/latest/source.py").data,
        "base64",
      ).toString(),
      source,
    );
    const bad = await execute("echo failure > outputs/failed.txt; exit 7", "bash");
    assert.equal(bad.outcome, "execution_error");
    assert.equal(bad.exit_code, 7);
    shellReady = false;
    assert.deepEqual(await start(), { ready: true, absent: true });
    const cold = await execute(
      "from pathlib import Path\nassert not Path('scratch.bin').exists()\nassert Path('outputs/result.txt').read_text()=='BETA\\n'\nprint('cold restored')",
    );
    assert.equal(cold.outcome, "ok", cold.stderr);
    const rootPython = await execute(
      "from pathlib import Path\nimport os\nPath('work/fmt_results.json').write_text('{\"value\":42}')\nos.chdir('work')",
      "python",
      { cwd: "." },
    );
    assert.equal(rootPython.outcome, "ok", rootPython.stderr);
    const rootBash = await execute(
      "test -d work && test -d scripts && jq .value work/fmt_results.json; cd work",
      "bash",
      { cwd: "." },
    );
    assert.equal(rootBash.outcome, "ok", rootBash.stderr);
    assert.equal(rootBash.stdout.trim(), "42");
    const rootAgain = await execute(
      "from pathlib import Path\nimport json\nassert json.loads(Path('work/fmt_results.json').read_text())['value']==42\nassert Path('outputs/result.txt').read_text()=='BETA\\n'",
      "python",
      { cwd: "." },
    );
    assert.equal(rootAgain.outcome, "ok", rootAgain.stderr);
    const deletedCwd = await execute(
      "import shutil\nshutil.rmtree('/home/pyodide', ignore_errors=True)\nprint('cwd removed')",
    );
    assert.equal(deletedCwd.outcome, "ok", deletedCwd.stderr);
    const afterCwd = await execute("print('busy flag cleared')");
    assert.equal(afterCwd.outcome, "ok", afterCwd.stderr);
    const recipeLink = await execute(
      "from pathlib import Path\nimport shutil\np=Path('scripts/.runs/latest');shutil.rmtree(p);p.symlink_to('../..')",
    );
    assert.equal(recipeLink.outcome, "snapshot_error");
    const link = await execute(
      "from pathlib import Path\nPath('../scripts/escape').symlink_to('/etc/passwd')",
    );
    assert.equal(link.outcome, "snapshot_error");
    const alias = await execute(
      "from pathlib import Path\np=Path('outputs');p.unlink();p.symlink_to('/tmp')",
    );
    assert.equal(alias.outcome, "snapshot_error");
    assert.deepEqual(await start(), { ready: true, absent: true });
    const legacyOperation = randomUUID();
    const legacyConfig = {
      operation_id: legacyOperation,
      key: "legacy",
      head: "",
      phase: "restore",
      warm: false,
      code: "print('legacy preserved')",
      language: "python",
      cwd: "work",
      baseline: [],
      inputs: [],
      blocked_modules: [],
      legacy_paths: ["scripts/.runs"],
      restore: [
        {
          path: "scripts/.runs",
          data: Buffer.from("legacy recipe-name collision").toString("base64"),
        },
      ],
    };
    assert.equal((await run(legacyConfig)).outcome || "ok", "ok");
    const legacy = await run({
      ...legacyConfig,
      phase: "execute",
      continuation: true,
      restore: [],
    });
    assert.equal(legacy.outcome, "ok", legacy.stderr);
    assert(legacy.manifest.some((file) => file.path === "scripts/.runs"));
    assert(legacy.manifest.some((file) => file.path === "scripts/.runs-current/latest/source.py"));
    const pending = {
      operation_id: randomUUID(),
      key: "pending",
      head: "",
      phase: "execute",
      warm: false,
      code: "from pathlib import Path\np=Path('scripts/count');p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\nPath('outputs/large').write_bytes(bytes(32768))\nprint('completed before snapshot')",
      language: "python",
      cwd: ".",
      restore: [],
      baseline: [],
      legacy_paths: [],
      inputs: [],
      blocked_modules: [],
      limits: {
        max_files: 64,
        max_file_bytes: 16384,
        max_total_bytes: 8 * 1024 * 1024,
        max_envelope_bytes: 15 * 1024 * 1024,
        max_text_bytes: 16384,
        callback_timeout_seconds: 55,
      },
    };
    const oversized = await run(pending);
    assert.equal(oversized.outcome, "snapshot_error");
    assert.equal(oversized.execution_completed, true);
    assert.equal(oversized.exit_code, 0);
    assert.equal(oversized.stdout.trim(), "completed before snapshot");
    const recovered = await run({
      ...pending,
      phase: "recover",
      code: "raise Exception('never run')",
      limits: { ...pending.limits, max_file_bytes: 65536 },
    });
    assert.equal(recovered.outcome, "ok");
    assert.equal(recovered.stdout, oversized.stdout);
    assert.equal(
      Buffer.from(
        recovered.files.find((file) => file.path === "scripts/count").data,
        "base64",
      ).toString(),
      "1",
    );
    assert.deepEqual(await start(), { ready: true, absent: true });
    const lost = await run({ ...pending, phase: "recover" });
    assert.equal(lost.outcome, "recovery_unavailable");
    const metadataFailure = await run({
      ...pending,
      operation_id: randomUUID(),
      key: "metadata",
      code: "import importlib.metadata\nprint('completion before metadata')\nimportlib.metadata.distributions=lambda: (_ for _ in ()).throw(RuntimeError('synthetic inventory failure'))",
    });
    assert.equal(metadataFailure.outcome, "snapshot_error");
    assert.equal(metadataFailure.execution_completed, true);
    assert.equal(metadataFailure.stdout.trim(), "completion before metadata");
    // Reset the deliberately altered interpreter before the next independent case.
    assert.deepEqual(await start(), { ready: true, absent: true });
    const clean = await run({
      ...pending,
      operation_id: randomUUID(),
      key: "evidence",
      code: "print('old evidence')",
    });
    assert.equal(clean.outcome, "ok");
    const preparedOperation = randomUUID();
    const prepared = await run({
      ...pending,
      operation_id: preparedOperation,
      key: "evidence",
      head: clean.operation_id,
      phase: "prepare",
    });
    assert.equal(prepared.warm, true);
    const advanced = await run({
      ...pending,
      operation_id: randomUUID(),
      key: "evidence",
      head: clean.operation_id,
      warm: true,
      code: "print('another operation completed')",
    });
    assert.equal(advanced.outcome, "ok");
    const stalePreparation = await run({
      ...pending,
      operation_id: preparedOperation,
      key: "evidence",
      head: clean.operation_id,
      warm: prepared.warm,
      code: "print('never run')",
    });
    assert.equal(stalePreparation.outcome, "snapshot_error");
    assert.equal(stalePreparation.execution_completed, undefined);
    assert.equal(stalePreparation.stdout, undefined);
    const beforeExecution = await run({
      ...pending,
      operation_id: randomUUID(),
      key: "evidence",
      head: advanced.operation_id,
      warm: true,
      cwd: "missing",
      code: "print('never run')",
    });
    assert.equal(beforeExecution.outcome, "snapshot_error");
    assert.equal(beforeExecution.execution_completed, undefined);
    assert.equal(beforeExecution.stdout, undefined);
  } finally {
    await browser?.close();
    await new Promise((resolve) => server.close(resolve));
  }
});
