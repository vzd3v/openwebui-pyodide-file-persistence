/** Maintainer build only. Consumers install the resulting single Python Tool. */
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const destination = resolve(root, "build/browser-shell.js");
await mkdir(dirname(destination), { recursive: true });
const options = {
  entryPoints: [resolve(root, "browser/workspace-shell.ts")],
  bundle: true,
  platform: "browser",
  format: "iife",
  minify: true,
  legalComments: "inline",
  write: false,
  plugins: [
    {
      name: "unsupported-browser-compression",
      setup(builder) {
        builder.onResolve({ filter: /^node:zlib$/ }, () => ({
          path: "compression",
          namespace: "unsupported",
        }));
        builder.onLoad({ filter: /.*/, namespace: "unsupported" }, () => ({
          contents:
            'export const constants={}; export function gzipSync(){throw Error("Compression is not supported in this browser shell")} export function gunzipSync(){throw Error("Compressed search is not supported in this browser shell")}',
          loader: "js",
        }));
      },
    },
  ],
};
const first = (await build(options)).outputFiles[0].contents;
const second = (await build(options)).outputFiles[0].contents;
if (!Buffer.from(first).equals(second)) throw new Error("Browser build is not deterministic");
const noticeParts = [await readFile(resolve(root, "THIRD_PARTY_NOTICES.txt"), "utf8")];
for (const name of [
  "just-bash",
  "diff",
  "sprintf-js",
  "turndown",
  "re2js",
  "ieee754",
  "buffer",
  "base64-js",
]) {
  const directory = resolve(root, "node_modules", name);
  const metadata = JSON.parse(await readFile(resolve(directory, "package.json"), "utf8"));
  noticeParts.push(
    `${name}@${metadata.version} (${metadata.license})\n${await readFile(resolve(directory, "LICENSE"), "utf8")}`,
  );
}
const notices = noticeParts.join("\n\n");
await writeFile(
  destination,
  `/*\n${notices.replaceAll("*/", "* /")}\n*/\n${Buffer.from(first).toString()}`,
);
const bytes = await readFile(destination);
console.log(
  `Built build/browser-shell.js sha256=${createHash("sha256").update(bytes).digest("hex")}`,
);
