import type {
  BufferEncoding,
  CpOptions,
  CreateExclusiveOptions,
  FileContent,
  FsStat,
  IFileSystem,
  MkdirOptions,
  RmOptions,
} from "just-bash/browser";

interface ReadFileOptions {
  encoding?: BufferEncoding;
}
interface WriteFileOptions {
  encoding?: BufferEncoding;
}
interface DirentEntry {
  name: string;
  isFile: boolean;
  isDirectory: boolean;
  isSymbolicLink: boolean;
}

interface NativeStat {
  mode: number;
  size: number;
  mtime: Date;
  ino: number;
  dev: number;
}
/** Public Emscripten FS operations exposed by pyodide_js.FS. */
export interface PyodideFS {
  readFile(path: string): Uint8Array;
  writeFile(path: string, bytes: Uint8Array, options?: { flags?: string }): void;
  stat(path: string): NativeStat;
  lstat(path: string): NativeStat;
  mkdir(path: string, mode?: number): void;
  readdir(path: string): string[];
  rmdir(path: string): void;
  unlink(path: string): void;
  rename(source: string, target: string): void;
  chmod(path: string, mode: number): void;
  symlink(target: string, link: string): void;
  readlink(path: string): string;
  utime(path: string, atime: number, mtime: number): void;
  isFile(mode: number): boolean;
  isDir(mode: number): boolean;
  isLink(mode: number): boolean;
}
const encoder = new TextEncoder();
const decoder = new TextDecoder();
const directory = (path: string): string => path.slice(0, path.lastIndexOf("/")) || "/";

function fault(code: string, path: string): Error {
  return Object.assign(new Error(`${code}: ${path}`), { code });
}
function encoding(options?: BufferEncoding | ReadFileOptions | WriteFileOptions): BufferEncoding {
  return typeof options === "string" ? options : options?.encoding || "utf8";
}
function decode(bytes: Uint8Array, kind: BufferEncoding): string {
  if (kind === "utf8" || kind === "utf-8") return decoder.decode(bytes);
  if (kind === "hex") return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  let raw = "";
  for (let i = 0; i < bytes.length; i += 8192) {
    raw += String.fromCharCode(...bytes.subarray(i, i + 8192));
  }
  if (kind === "base64") return btoa(raw);
  if (kind === "ascii") return Array.from(bytes, (b) => String.fromCharCode(b & 127)).join("");
  return raw;
}
function encode(value: FileContent, kind: BufferEncoding): Uint8Array {
  if (value instanceof Uint8Array) return value;
  if (kind === "utf8" || kind === "utf-8") return encoder.encode(value);
  if (kind === "hex") {
    if (value.length % 2 || !/^[0-9a-f]*$/i.test(value)) throw fault("EINVAL", "hex encoding");
    return Uint8Array.from(value.match(/../g) || [], (p) => Number.parseInt(p, 16));
  }
  const raw = kind === "base64" ? atob(value) : value;
  return Uint8Array.from(raw, (c) => c.charCodeAt(0) & (kind === "ascii" ? 127 : 255));
}

/** A single filesystem, rooted at one managed workspace. No file mirroring. */
export class WorkspaceFS implements IFileSystem {
  private readonly root: string;
  constructor(
    private readonly native: PyodideFS,
    root: string,
    private readonly readonlyInputs = true,
  ) {
    if (!root.startsWith("/") || root.endsWith("/") || root.includes("/../")) {
      throw fault("EINVAL", "workspace root");
    }
    this.root = root;
    const codes: Record<number, string> = {
      2: "EACCES",
      20: "EEXIST",
      28: "EINVAL",
      31: "EISDIR",
      32: "ELOOP",
      44: "ENOENT",
      54: "ENOTDIR",
      55: "ENOTEMPTY",
      63: "EPERM",
      69: "EROFS",
    };
    // Emscripten uses its documented errno numbers; shell commands expect
    // conventional errno names in errors. Preserve unexpected native errors.
    this.native = new Proxy(native, {
      get(target, key) {
        const value: unknown = Reflect.get(target, key);
        if (typeof value !== "function") return value;
        return (...args: unknown[]) => {
          try {
            return Reflect.apply(value, target, args);
          } catch (error) {
            if (
              error &&
              typeof error === "object" &&
              "errno" in error &&
              typeof error.errno === "number" &&
              codes[error.errno]
            ) {
              throw fault(codes[error.errno], String(args[0] ?? "filesystem"));
            }
            throw error;
          }
        };
      },
    });
  }

  resolvePath(base: string, value: string): string {
    if (value.includes("\0")) throw fault("EINVAL", "NUL path");
    const parts: string[] = [];
    for (const part of (value.startsWith("/") ? value : `${base}/${value}`).split("/")) {
      if (!part || part === ".") continue;
      if (part === "..") {
        parts.pop();
      } else parts.push(part);
    }
    return `/${parts.join("/")}`;
  }

  private resolveLink(base: string, target: string): string {
    let depth = base.split("/").filter(Boolean).length;
    for (const part of target.split("/")) {
      if (part === "..") {
        if (--depth < 0) throw fault("EACCES", "link leaves workspace");
      } else if (part && part !== ".") depth++;
    }
    return this.resolvePath(base, target);
  }

  private path(value: string, follow = true): string {
    const logical = this.resolvePath("/", value);
    // Resolve existing symlinks ourselves through public FS methods, including
    // dangling final targets on writes. Never let native FS resolve an escape.
    let parts = logical.split("/").filter(Boolean);
    let done: string[] = [];
    let links = 0;
    while (parts.length) {
      const part = parts.shift();
      if (part === undefined) break;
      done.push(part);
      const candidate = `${this.root}/${done.join("/")}`;
      if (!follow && parts.length === 0) break;
      let metadata: NativeStat;
      try {
        metadata = this.native.lstat(candidate);
      } catch (error) {
        if (this.isMissing(error)) continue;
        throw error;
      }
      if (this.native.isLink(metadata.mode)) {
        if (++links > 40) throw fault("ELOOP", logical);
        const target = this.native.readlink(candidate);
        // Links created by Python are native paths. Absolute targets must be
        // under this root, not under the browser runtime's /etc or /home.
        const destination = target.startsWith("/")
          ? target === this.root
            ? "/"
            : target.startsWith(`${this.root}/`)
              ? target.slice(this.root.length)
              : (() => {
                  throw fault("EACCES", logical);
                })()
          : this.resolveLink(`/${done.slice(0, -1).join("/")}`, target);
        parts = [...destination.split("/").filter(Boolean), ...parts];
        done = [];
      }
    }
    return `${this.root}${done.length ? `/${done.join("/")}` : ""}`;
  }

  private isMissing(error: unknown): boolean {
    return (
      !!error &&
      typeof error === "object" &&
      (("code" in error && error.code === "ENOENT") || ("errno" in error && error.errno === 44))
    );
  }
  private checked(value: string, write = false, follow = true): string {
    const path = this.path(value, follow);
    if (
      write &&
      this.readonlyInputs &&
      (path === `${this.root}/inputs` || path.startsWith(`${this.root}/inputs/`))
    ) {
      throw fault("EROFS", value);
    }
    return path;
  }
  private info(path: string, follow: boolean): FsStat {
    const metadata = follow
      ? this.native.stat(this.path(path))
      : this.native.lstat(this.path(path, false));
    return {
      ...metadata,
      isFile: this.native.isFile(metadata.mode),
      isDirectory: this.native.isDir(metadata.mode),
      isSymbolicLink: this.native.isLink(metadata.mode),
    };
  }
  async readFile(path: string, options?: ReadFileOptions | BufferEncoding): Promise<string> {
    return decode(await this.readFileBuffer(path), encoding(options));
  }
  async readFileBuffer(path: string): Promise<Uint8Array> {
    return this.native.readFile(this.checked(path));
  }
  async writeFile(
    path: string,
    content: FileContent,
    options?: WriteFileOptions | BufferEncoding,
  ): Promise<void> {
    this.native.writeFile(this.checked(path, true), encode(content, encoding(options)));
  }
  async appendFile(
    path: string,
    content: FileContent,
    options?: WriteFileOptions | BufferEncoding,
  ): Promise<void> {
    this.native.writeFile(this.checked(path, true), encode(content, encoding(options)), {
      flags: "a",
    });
  }
  async exists(path: string): Promise<boolean> {
    try {
      this.native.lstat(this.checked(path));
      return true;
    } catch (error) {
      if (this.isMissing(error)) return false;
      throw error;
    }
  }
  async stat(path: string): Promise<FsStat> {
    return this.info(path, true);
  }
  async lstat(path: string): Promise<FsStat> {
    return this.info(path, false);
  }
  async mkdir(path: string, options?: MkdirOptions): Promise<void> {
    const target = this.checked(path, true, false);
    if (!options?.recursive) {
      this.native.mkdir(target);
      return;
    }
    const relative = target.slice(this.root.length);
    let current = "/";
    for (const component of relative.split("/").filter(Boolean)) {
      current = this.resolvePath(current, component);
      const native = this.checked(current, true);
      try {
        this.native.mkdir(native);
      } catch (error) {
        if (!this.native.isDir(this.native.stat(native).mode)) throw error;
      }
    }
  }
  async createExclusive(path: string, options: CreateExclusiveOptions): Promise<void> {
    const target = this.checked(path, true, false);
    if (options.directory) {
      this.native.mkdir(target, options.mode);
      return;
    }
    this.native.writeFile(target, new Uint8Array(), { flags: "wx" });
    this.native.chmod(target, options.mode);
  }
  async readdir(path: string): Promise<string[]> {
    return this.native.readdir(this.checked(path)).filter((name) => name !== "." && name !== "..");
  }
  async readdirWithFileTypes(path: string): Promise<DirentEntry[]> {
    return Promise.all(
      (await this.readdir(path)).map(async (name) => {
        const metadata = await this.lstat(this.resolvePath(path, name));
        return {
          name,
          isFile: metadata.isFile,
          isDirectory: metadata.isDirectory,
          isSymbolicLink: metadata.isSymbolicLink,
        };
      }),
    );
  }
  async rm(path: string, options?: RmOptions): Promise<void> {
    const target = this.checked(path, true, false);
    if (target === this.root) throw fault("EACCES", "workspace root");
    let metadata: NativeStat;
    try {
      metadata = this.native.lstat(target);
    } catch (error) {
      if (options?.force && this.isMissing(error)) return;
      throw error;
    }
    if (this.native.isDir(metadata.mode)) {
      if (options?.recursive)
        for (const name of await this.readdir(path))
          await this.rm(this.resolvePath(path, name), options);
      this.native.rmdir(target);
    } else this.native.unlink(target);
  }
  async cp(source: string, destination: string, options?: CpOptions): Promise<void> {
    const src = this.resolvePath("/", source);
    const dst = this.resolvePath("/", destination);
    if (src === dst || dst.startsWith(`${src}/`)) throw fault("EINVAL", "copy into itself");
    const metadata = await this.lstat(src);
    if (metadata.isSymbolicLink) {
      await this.symlink(await this.readlink(src), dst);
      return;
    } else if (metadata.isDirectory) {
      if (!options?.recursive) throw fault("EISDIR", source);
      await this.mkdir(dst, { recursive: true });
      for (const name of await this.readdir(src))
        await this.cp(this.resolvePath(src, name), this.resolvePath(dst, name), options);
    } else await this.writeFile(dst, await this.readFileBuffer(src));
    await this.chmod(dst, metadata.mode & 0o777);
    await this.utimes(dst, metadata.mtime, metadata.mtime);
  }
  async mv(source: string, destination: string): Promise<void> {
    const src = this.checked(source, true, false);
    const dst = this.checked(destination, true, false);
    if (src === this.root || dst === this.root) throw fault("EACCES", "workspace root");
    this.native.rename(src, dst);
  }
  getAllPaths(): string[] {
    const results: string[] = [];
    const walk = (path: string): void => {
      for (const name of this.native
        .readdir(this.path(path))
        .filter((p) => p !== "." && p !== "..")) {
        const child = this.resolvePath(path, name);
        results.push(child);
        if (this.native.isDir(this.native.lstat(this.path(child, false)).mode)) walk(child);
      }
    };
    walk("/");
    return results;
  }
  async chmod(path: string, mode: number): Promise<void> {
    this.native.chmod(this.checked(path, true), mode);
  }
  async symlink(target: string, link: string): Promise<void> {
    const logical = target.startsWith("/")
      ? this.resolvePath("/", target)
      : this.resolveLink(directory(link), target);
    this.path(logical); // Reject chains already escaping the root.
    this.native.symlink(
      target.startsWith("/") ? `${this.root}${logical}` : target,
      this.checked(link, true, false),
    );
  }
  async link(_existing: string, _newPath: string): Promise<void> {
    throw fault("ENOSYS", "hard links are not supported by Emscripten FS");
  }
  async readlink(path: string): Promise<string> {
    const value = this.native.readlink(this.checked(path, false, false));
    if (!value.startsWith("/")) return value;
    if (value === this.root) return "/";
    if (!value.startsWith(`${this.root}/`)) throw fault("EACCES", path);
    return value.slice(this.root.length);
  }
  async realpath(path: string): Promise<string> {
    return this.path(path).slice(this.root.length) || "/";
  }
  async utimes(path: string, atime: Date, mtime: Date): Promise<void> {
    this.native.utime(this.checked(path, true), atime.getTime(), mtime.getTime());
  }
}
