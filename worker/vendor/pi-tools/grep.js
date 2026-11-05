// Derived from @earendil-works/pi-coding-agent v1.5.0 (MIT). See provenance.json and LICENSE.
import path from "./remote-path.js";
import { Type } from "typebox";
import { resolveToCwd } from "./remote-path.js";
import { DEFAULT_MAX_BYTES, formatSize, GREP_MAX_LINE_LENGTH, truncateHead, truncateLine, } from "./truncate.js";
const grepSchema = Type.Object({
    pattern: Type.String({ description: "Search pattern (regex or literal string)" }),
    path: Type.Optional(Type.String({ description: "Directory or file to search (default: current directory)" })),
    glob: Type.Optional(Type.String({ description: "Filter files by glob pattern, e.g. '*.ts' or '**/*.spec.ts'" })),
    ignoreCase: Type.Optional(Type.Boolean({ description: "Case-insensitive search (default: false)" })),
    literal: Type.Optional(Type.Boolean({ description: "Treat pattern as literal string instead of regex (default: false)" })),
    context: Type.Optional(Type.Number({ description: "Number of lines to show before and after each match (default: 0)" })),
    limit: Type.Optional(Type.Number({ description: "Maximum number of matches to return (default: 100)" })),
});
export const grepToolSystemPromptContribution = {
    snippet: "Search file contents for patterns (respects .gitignore)",
    guidelines: [],
};
const DEFAULT_LIMIT = 100;
export function createGrepToolDefinition(cwd, options) {
    const customOps = options?.operations;
    return {
        name: "grep",
        label: "grep",
        description: `Search file contents for a pattern. Returns matching lines with file paths and line numbers. Respects .gitignore. Output is truncated to ${DEFAULT_LIMIT} matches or ${DEFAULT_MAX_BYTES / 1024}KB (whichever is hit first). Long lines are truncated to ${GREP_MAX_LINE_LENGTH} chars.`,
        promptSnippet: grepToolSystemPromptContribution.snippet,
        parameters: grepSchema,
        async execute(_toolCallId, { pattern, path: searchDir, glob, ignoreCase, literal, context, limit, }, signal, _onUpdate, ctx) {
            throw new Error("Remote grep execute must be installed");
        },
    };
}
