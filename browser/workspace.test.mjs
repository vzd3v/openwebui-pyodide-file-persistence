import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
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
  const nativeFiles = new Map();
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://localhost");
      if (url.pathname.startsWith("/api/v1/files/")) {
        if (!request.headers.cookie?.includes("token=synthetic-cookie")) {
          response.writeHead(401).end();
          return;
        }
        if (
          request.method === "POST" &&
          url.pathname === "/api/v1/files/" &&
          url.searchParams.get("process") === "false"
        ) {
          const chunks = [];
          for await (const chunk of request) chunks.push(chunk);
          const form = await new Response(Buffer.concat(chunks), {
            headers: {
              "content-type": request.headers["content-type"],
            },
          }).formData();
          const id = randomUUID();
          nativeFiles.set(id, {
            data: Buffer.from(await form.get("file").arrayBuffer()),
            metadata: JSON.parse(form.get("metadata")),
          });
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ id }));
          return;
        }
        if (url.searchParams.has("disconnect")) {
          request.socket.destroy();
          return;
        }
        const id = url.pathname.split("/")[4];
        if (request.method === "GET" && nativeFiles.has(id)) {
          if (url.searchParams.has("delay"))
            await new Promise((resolve) => setTimeout(resolve, 200));
          response.setHeader("Content-Type", "application/octet-stream");
          response.end(nativeFiles.get(id).data);
          return;
        }
        response.writeHead(404).end();
        return;
      }
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
    const initialProbe = await run({
      operation_id: randomUUID(),
      key: "synthetic",
      head: "",
      phase: "probe",
      restore: [],
    });
    assert.match(initialProbe.runtime_id, /^[0-9a-f-]{36}$/);
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
        runtime_id: prepared.runtime_id,
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
      if (language === "python") {
        const packages = await run({
          ...config,
          phase: "packages",
          code: "",
          package_source: Buffer.from(code).toString("base64"),
        });
        assert.equal(packages.outcome || "ok", "ok", JSON.stringify(packages));
      }
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
      const completion = await run({ ...config, defer_snapshot: true });
      const value =
        completion.outcome === "snapshot_error"
          ? completion
          : await run({
              ...config,
              phase: "snapshot",
              code: "",
              include_shell: false,
            });
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
    const packageConfig = {
      operation_id: randomUUID(),
      key: "packages",
      head: "",
      phase: "packages",
      code: "",
      blocked_modules: ["subprocess"],
    };
    // Local test assets deliberately omit catalog wheels. The public loader can
    // resolve its Promise despite the 404; the wrapper must retain the failure.
    const unavailablePackage = await run({
      ...packageConfig,
      package_source: Buffer.from("from PIL import Image").toString("base64"),
    });
    assert.equal(unavailablePackage.outcome, "dependency_error");
    const blockedPackage = await run({
      ...packageConfig,
      package_source: Buffer.from("import subprocess").toString("base64"),
    });
    assert.equal(blockedPackage.outcome || "ok", "ok");
    const invalidSyntax = await execute("import PIL\nif (");
    assert.equal(invalidSyntax.outcome, "execution_error");
    assert.match(invalidSyntax.stderr, /SyntaxError/);
    assert.equal((await execute("ephemeral_name = 123")).outcome, "ok");
    const freshGlobals = await execute("print(ephemeral_name)");
    assert.equal(freshGlobals.outcome, "execution_error");
    assert.match(freshGlobals.stderr, /NameError/);
    const first = await execute(
      'from pathlib import Path\nPath(\'scratch.bin\').write_bytes(bytes(256*1024*1024))\nPath(\'../scripts/data.json\').write_text(\'{"items":[{"name":"α","n":2},{"name":"β","n":3}]}\',encoding=\'utf-8\')\nprint(\'python ready\')',
    );
    assert.equal(first.outcome, "ok", first.stderr);
    assert.equal(first.runtime_id, initialProbe.runtime_id);
    const wrongRuntime = await run({
      operation_id: randomUUID(),
      key: "synthetic",
      head,
      phase: "execute",
      runtime_id: randomUUID(),
      restore: [],
      code: "raise AssertionError('must not execute')",
    });
    assert.equal(wrongRuntime.outcome, "runtime_changed");
    assert.equal(wrongRuntime.execution_completed, undefined);
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
    const boundedLogs = await execute(
      "import sys\nfor _ in range(30000): sys.stdout.write('é'*32)",
    );
    assert.equal(boundedLogs.stdout_truncated, true);
    const logSizes = await execute(
      "from pathlib import Path\nlogs=list(Path('.').glob('*.stdout'))\nassert logs and all(p.stat().st_size<=16384 for p in logs)\nprint('scratch logs bounded')",
    );
    assert.equal(logSizes.outcome, "ok", logSizes.stderr);
    const bad = await execute("echo failure > outputs/failed.txt; exit 7", "bash");
    assert.equal(bad.outcome, "execution_error");
    assert.equal(bad.exit_code, 7);
    shellReady = false;
    assert.deepEqual(await start(), { ready: true, absent: true });
    const cold = await execute(
      "from pathlib import Path\nassert not Path('scratch.bin').exists()\nassert Path('outputs/result.txt').read_text()=='BETA\\n'\nprint('cold restored')",
    );
    assert.equal(cold.outcome, "ok", cold.stderr);
    assert.notEqual(cold.runtime_id, initialProbe.runtime_id);
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
    assert.equal(recipeLink.cause.code, "retained_link");
    assert.equal(recipeLink.cause.exception_type, "ValueError");
    assert.equal(recipeLink.cause.frame, "managed_workspace");
    assert.equal(typeof recipeLink.cause.line, "number");
    assert.equal(Object.hasOwn(recipeLink.cause, "message"), false);
    const link = await execute(
      "from pathlib import Path\nPath('../scripts/escape').symlink_to('/etc/passwd')",
    );
    assert.equal(link.outcome, "snapshot_error");
    const alias = await execute(
      "from pathlib import Path\np=Path('outputs');p.unlink();p.symlink_to('/tmp')",
    );
    assert.equal(alias.outcome, "snapshot_error");
    assert.equal(alias.cause.code, "managed_alias");
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
    const acknowledged = await run({ ...pending, defer_snapshot: true });
    assert.equal(acknowledged.outcome, "ok");
    assert.equal(acknowledged.execution_completed, true);
    assert.equal(acknowledged.manifest, undefined);
    assert.equal(acknowledged.files, undefined);
    const oversized = await run({
      ...pending,
      phase: "snapshot",
      code: "raise Exception('never run')",
    });
    assert.equal(oversized.outcome, "snapshot_error");
    assert.equal(oversized.cause.code, "retained_file_limit");
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
    // Production wrapper, binary native HTTP boundary, real Pyodide and cookie auth.
    assert.deepEqual(await start(), { ready: true, absent: true });
    await page
      .context()
      .addCookies([{ name: "token", value: "synthetic-cookie", url: page.url(), httpOnly: true }]);
    const inputId = randomUUID();
    const inputBytes = Buffer.alloc(20 * 1024 * 1024, 173);
    const sha256 = createHash("sha256").update(inputBytes).digest("hex");
    nativeFiles.set(inputId, { data: inputBytes });
    const nativeInput = {
      path: `inputs/${inputId}/large.bin`,
      file_id: inputId,
      sha256,
      size: inputBytes.length,
      committed: true,
    };
    const binaryConfig = {
      operation_id: randomUUID(),
      key: "binary",
      head: "",
      phase: "restore",
      warm: false,
      code: "",
      language: "python",
      cwd: ".",
      restore: [],
      binary_transport: true,
      baseline: [],
      legacy_paths: [],
      inputs: [nativeInput],
      blocked_modules: [],
      native_restore: [{ ...nativeInput, url: `/api/v1/files/${inputId}/content` }],
    };
    const timedRestore = await run({
      ...binaryConfig,
      operation_id: randomUUID(),
      key: "binary-deadline",
      http_timeout_seconds: 0.01,
      native_restore: [{ ...nativeInput, url: `/api/v1/files/${inputId}/content?delay=1` }],
    });
    assert.equal(timedRestore.outcome, "snapshot_error", JSON.stringify(timedRestore));
    assert.equal(timedRestore.cause.code, "transfer_timeout");
    const disconnectedRestore = await run({
      ...binaryConfig,
      operation_id: randomUUID(),
      key: "binary-disconnected",
      native_restore: [{ ...nativeInput, url: `/api/v1/files/${inputId}/content?disconnect=1` }],
    });
    assert.equal(disconnectedRestore.outcome, "snapshot_error");
    assert.equal(disconnectedRestore.cause.code, "transfer_request_failed");
    const binaryRestored = await run(binaryConfig);
    assert.notEqual(binaryRestored.outcome, "snapshot_error", JSON.stringify(binaryRestored));
    const binaryComplete = await run({
      ...binaryConfig,
      phase: "execute",
      continuation: true,
      native_restore: [],
      defer_snapshot: true,
      code: `from pathlib import Path\np=Path('scripts/count')\np.write_text(str(int(p.read_text())+1) if p.exists() else '1')\nPath('outputs/large.bin').write_bytes(Path('${nativeInput.path}').read_bytes())`,
    });
    assert.equal(binaryComplete.outcome, "ok", JSON.stringify(binaryComplete));
    const binarySnapshot = await run({ ...binaryConfig, phase: "snapshot", native_restore: [] });
    assert.equal(binarySnapshot.binary, true);
    assert.equal(binarySnapshot.outcome, "ok", JSON.stringify(binarySnapshot));
    assert.deepEqual(binarySnapshot.files, []);
    assert(JSON.stringify(binarySnapshot).length < 20000);
    const large = binarySnapshot.manifest.find((item) => item.path === "outputs/large.bin");
    assert.equal(large.sha256, sha256);
    const transfer = { ...large, transfer_key: randomUUID() };
    const uploadConfig = {
      ...binaryConfig,
      phase: "upload",
      native_restore: [],
      native_transfer: transfer,
      upload_url: "/api/v1/files/?process=false",
    };
    const missingRoute = await run({ ...uploadConfig, upload_url: "/api/v1/files/missing" });
    assert.equal(missingRoute.outcome, "snapshot_error");
    assert.equal(missingRoute.cause.code, "transfer_http");
    assert.equal(missingRoute.cause.http_status, 404);
    const uploaded = await run(uploadConfig);
    assert.equal(uploaded.outcome, "ok", JSON.stringify(uploaded));
    assert(JSON.stringify(uploaded).length < 2048);
    assert.match(uploaded.uploaded_file_id, /^[0-9a-f-]{36}$/);
    const stored = nativeFiles.get(uploaded.uploaded_file_id);
    assert.equal(createHash("sha256").update(stored.data).digest("hex"), sha256);
    assert.deepEqual(stored.metadata.pyodide_file_saver, {
      operation_id: binaryConfig.operation_id,
      transfer_key: transfer.transfer_key,
    });
    const changedReservation = await run({
      ...uploadConfig,
      native_transfer: { ...transfer, sha256: "0".repeat(64) },
    });
    assert.equal(changedReservation.cause.code, "transfer_changed");
    const recoveredBinary = await run({ ...binaryConfig, phase: "recover", native_restore: [] });
    assert.equal(recoveredBinary.outcome, "ok");
    const count = recoveredBinary.manifest.find((item) => item.path === "scripts/count");
    assert.equal(count.sha256, createHash("sha256").update("1").digest("hex"));
    assert.deepEqual(await start(), { ready: true, absent: true });
    const binaryCold = {
      ...binaryConfig,
      operation_id: randomUUID(),
      key: "binary-cold",
      inputs: [],
      native_restore: [{ ...large, url: `/api/v1/files/${uploaded.uploaded_file_id}/content` }],
    };
    assert.notEqual((await run(binaryCold)).outcome, "snapshot_error");
    const coldResult = await run({
      ...binaryCold,
      phase: "execute",
      continuation: true,
      native_restore: [],
      code: "from pathlib import Path\nprint(len(Path('outputs/large.bin').read_bytes()))",
    });
    assert.equal(coldResult.outcome, "ok", JSON.stringify(coldResult));
    assert.equal(coldResult.stdout.trim(), String(inputBytes.length));
  } finally {
    await browser?.close();
    await new Promise((resolve) => server.close(resolve));
  }
});
