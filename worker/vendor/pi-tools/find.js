// Derived from @earendil-works/pi-coding-agent v1.5.0 (MIT). See provenance.json and LICENSE.
import path from "./remote-path.js";
import { Type } from "typebox";
import { pathExists, resolveToCwd } from "./remote-path.js";
import { DEFAULT_MAX_BYTES, formatSize, truncateHead } from "./truncate.js";
/** Relativize a find result against the search root and normalize it to posix separators. */
export function relativizeFindResultPath(resultPath, searchPath, pathModule = path) {
    const hadTrailingSeparator = resultPath.endsWith(pathModule.sep) || (pathModule.sep === "\\" && resultPath.endsWith("/"));
    const relativePath = pathModule.isAbsolute(resultPath) ? pathModule.relative(searchPath, resultPath) : resultPath;
    const posixPath = relativePath.split(pathModule.sep).join("/");
    return hadTrailingSeparator && !posixPath.endsWith("/") ? `${posixPath}/` : posixPath;
}
const findSchema = Type.Object({
    pattern: Type.String({
        description: "Glob pattern to match files, e.g. '*.ts', '**/*.json', or 'src/**/*.spec.ts'",
    }),
    path: Type.Optional(Type.String({ description: "Directory to search in (default: current directory)" })),
    limit: Type.Optional(Type.Number({ description: "Maximum number of results (default: 1000)" })),
});
export const findToolSystemPromptContribution = {
    snippet: "Find files by glob pattern (respects .gitignore)",
    guidelines: [],
};
const DEFAULT_LIMIT = 1000;
export function createFindToolDefinition(cwd, options) {
    const customOps = options?.operations;
    return {
        name: "find",
        label: "find",
        description: `Search for files by glob pattern. Returns matching file paths relative to the search directory. Respects .gitignore. Output is truncated to ${DEFAULT_LIMIT} results or ${DEFAULT_MAX_BYTES / 1024}KB (whichever is hit first).`,
        promptSnippet: findToolSystemPromptContribution.snippet,
        parameters: findSchema,
        async execute(_toolCallId, { pattern, path: searchDir, limit }, signal, _onUpdate, ctx) {
            return new Promise((resolve, reject) => {
                if (signal?.aborted) {
                    reject(new Error("Operation aborted"));
                    return;
                }
                let settled = false;
                let stopChild;
                const settle = (fn) => {
                    if (settled)
                        return;
                    settled = true;
                    signal?.removeEventListener("abort", onAbort);
                    stopChild = undefined;
                    fn();
                };
                const onAbort = () => {
                    stopChild?.();
                    settle(() => reject(new Error("Operation aborted")));
                };
                signal?.addEventListener("abort", onAbort, { once: true });
                (async () => {
                    try {
                        const searchPath = resolveToCwd(searchDir || ".", ctx?.cwd || cwd);
                        const effectiveLimit = limit ?? DEFAULT_LIMIT;
                        const ops = customOps;
                        if (!(await ops.exists(searchPath))) {
                            settle(() => reject(new Error(`Path not found: ${searchPath}`)));
                            return;
                        }
                        if (signal?.aborted) {
                            settle(() => reject(new Error("Operation aborted")));
                            return;
                        }
                        const results = await ops.glob(pattern, searchPath, {
                            ignore: ["**/node_modules/**", "**/.git/**"],
                            limit: effectiveLimit,
                        });
                        if (signal?.aborted) {
                            settle(() => reject(new Error("Operation aborted")));
                            return;
                        }
                        if (results.length === 0) {
                            settle(() => resolve({
                                content: [{ type: "text", text: "No files found matching pattern" }],
                                details: undefined,
                            }));
                            return;
                        }
                        // Relativize paths against the search root for stable output.
                        const relativized = results.map((p) => relativizeFindResultPath(p, searchPath));
                        const resultLimitReached = relativized.length >= effectiveLimit;
                        const rawOutput = relativized.join("\n");
                        const truncation = truncateHead(rawOutput, { maxLines: Number.MAX_SAFE_INTEGER });
                        let resultOutput = truncation.content;
                        const details = {};
                        const notices = [];
                        if (resultLimitReached) {
                            notices.push(`${effectiveLimit} results limit reached`);
                            details.resultLimitReached = effectiveLimit;
                        }
                        if (truncation.truncated) {
                            notices.push(`${formatSize(DEFAULT_MAX_BYTES)} limit reached`);
                            details.truncation = truncation;
                        }
                        if (notices.length > 0) {
                            resultOutput += `\n\n[${notices.join(". ")}]`;
                        }
                        settle(() => resolve({
                            content: [{ type: "text", text: resultOutput }],
                            details: Object.keys(details).length > 0 ? details : undefined,
                        }));
                        return;
                    }
                    catch (e) {
                        if (signal?.aborted) {
                            settle(() => reject(new Error("Operation aborted")));
                            return;
                        }
                        const error = e instanceof Error ? e : new Error(String(e));
                        settle(() => reject(error));
                    }
                })();
            });
        },
    };
}
