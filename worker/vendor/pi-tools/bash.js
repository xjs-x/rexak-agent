// Derived from @earendil-works/pi-coding-agent v1.5.0 (MIT). See provenance.json and LICENSE.
import { getShellEnv } from "./remote-path.js";
const BASH_UPDATE_THROTTLE_MS = 100;
import { Type } from "typebox";
import { OutputAccumulator } from "./output-accumulator.js";
import { DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, formatSize } from "./truncate.js";
const MAX_TIMEOUT_MS = 2147483647;
/** Output limit of `structuredContent.output`, which programmatic callers such as codemode scripts receive. */
const STRUCTURED_OUTPUT_MAX_BYTES = 1024 * 1024;
const MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000;
function resolveTimeoutMs(timeout) {
    if (timeout === undefined)
        return undefined;
    if (!Number.isFinite(timeout) || timeout <= 0) {
        throw new Error("Invalid timeout: must be a finite number of seconds");
    }
    const timeoutMs = timeout * 1000;
    if (timeoutMs > MAX_TIMEOUT_MS) {
        throw new Error(`Invalid timeout: maximum is ${MAX_TIMEOUT_SECONDS} seconds`);
    }
    return timeoutMs;
}
const bashSchema = Type.Object({
    command: Type.String({ description: "Shell command to execute" }),
    timeout: Type.Optional(Type.Number({ description: "Timeout in seconds (optional, no default timeout)" })),
});
export const bashToolSystemPromptContribution = {
    snippet: "Execute bash commands (ls, grep, find, etc.)",
    guidelines: ["You can inspect PI_* environment variables for current model and session details."],
};
/**
 * Result for programmatic callers such as codemode scripts. A non-zero exit code is an error result for the model, but scripts still resolve to this value.
 * `output` is not limited like the model-facing output: callers decide how much of it reaches the model.
 */
const bashOutputSchema = Type.Object({
    output: Type.String({ description: "Combined stdout and stderr, possibly truncated" }),
    truncated: Type.Boolean(),
    full_output_path: Type.Optional(Type.String({ description: "Full output, when truncated" })),
    exit_code: Type.Number(),
    wall_time_seconds: Type.Number(),
});
function resolveSpawnContext(command, cwd, spawnHook, exposeSessionEnvironment, ctx) {
    const env = { ...getShellEnv() };
    delete env.PI_SESSION_ID;
    delete env.PI_SESSION_FILE;
    delete env.PI_PROVIDER;
    delete env.PI_MODEL;
    delete env.PI_REASONING_LEVEL;
    if (exposeSessionEnvironment && ctx) {
        const model = ctx.model;
        env.PI_SESSION_ID = ctx.sessionManager.getSessionId();
        const sessionFile = ctx.sessionManager.getSessionFile();
        if (sessionFile)
            env.PI_SESSION_FILE = sessionFile;
        if (model) {
            env.PI_PROVIDER = model.provider;
            env.PI_MODEL = model.id;
        }
        if (ctx.thinkingLevel)
            env.PI_REASONING_LEVEL = ctx.thinkingLevel;
    }
    const baseContext = { command, cwd, env };
    return spawnHook ? spawnHook(baseContext) : baseContext;
}
export function createShellToolDefinition(cwd, config, options) {
    const ops = options.operations;
    const commandPrefix = options?.commandPrefix;
    const exposeSessionEnvironment = options?.exposeSessionEnvironment ?? true;
    const spawnHook = options?.spawnHook;
    return {
        name: config.name,
        label: config.label,
        description: `Execute a ${config.shellName} command in the current working directory. Returns stdout and stderr. Output is truncated to last ${DEFAULT_MAX_LINES} lines or ${DEFAULT_MAX_BYTES / 1024}KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally provide a timeout in seconds.`,
        promptSnippet: config.promptSnippet,
        promptGuidelines: exposeSessionEnvironment && config.promptGuidelines ? [...config.promptGuidelines] : undefined,
        parameters: bashSchema,
        outputSchema: bashOutputSchema,
        constrainedSampling: { type: "json_schema", strict: "prefer" },
        async execute(_toolCallId, { command, timeout }, signal, onUpdate, ctx) {
            const resolvedCommand = commandPrefix ? `${commandPrefix}\n${command}` : command;
            const spawnContext = resolveSpawnContext(resolvedCommand, ctx?.cwd || cwd, spawnHook, exposeSessionEnvironment, ctx);
            const output = new OutputAccumulator({ tempFilePrefix: config.tempFilePrefix });
            let acceptingOutput = true;
            let updateTimer;
            let updateDirty = false;
            let lastUpdateAt = 0;
            const emitOutputUpdate = () => {
                if (!onUpdate || !updateDirty)
                    return;
                updateDirty = false;
                lastUpdateAt = Date.now();
                const snapshot = output.snapshot({ persistIfTruncated: true });
                onUpdate({
                    content: [{ type: "text", text: snapshot.content || "" }],
                    details: {
                        truncation: snapshot.truncation.truncated ? snapshot.truncation : undefined,
                        fullOutputPath: snapshot.fullOutputPath,
                    },
                });
            };
            const clearUpdateTimer = () => {
                if (updateTimer) {
                    clearTimeout(updateTimer);
                    updateTimer = undefined;
                }
            };
            const scheduleOutputUpdate = () => {
                if (!onUpdate)
                    return;
                updateDirty = true;
                const delay = BASH_UPDATE_THROTTLE_MS - (Date.now() - lastUpdateAt);
                if (delay <= 0) {
                    clearUpdateTimer();
                    emitOutputUpdate();
                    return;
                }
                updateTimer ??= setTimeout(() => {
                    updateTimer = undefined;
                    emitOutputUpdate();
                }, delay);
            };
            if (onUpdate) {
                onUpdate({ content: [], details: undefined });
            }
            const handleData = (data) => {
                if (!acceptingOutput)
                    return;
                output.append(data);
                scheduleOutputUpdate();
            };
            const finishOutput = async () => {
                acceptingOutput = false;
                output.finish();
                clearUpdateTimer();
                emitOutputUpdate();
                const snapshot = output.snapshot({ persistIfTruncated: true });
                await output.closeTempFile();
                return snapshot;
            };
            const formatOutput = (snapshot, emptyText = "(no output)") => {
                const truncation = snapshot.truncation;
                let text = snapshot.content || emptyText;
                let details;
                if (truncation.truncated) {
                    details = { truncation, fullOutputPath: snapshot.fullOutputPath };
                    const startLine = truncation.totalLines - truncation.outputLines + 1;
                    const endLine = truncation.totalLines;
                    if (truncation.lastLinePartial) {
                        const lastLineSize = formatSize(output.getLastLineBytes());
                        text += `\n\n[Showing last ${formatSize(truncation.outputBytes)} of line ${endLine} (line is ${lastLineSize}). Full output: ${snapshot.fullOutputPath}]`;
                    }
                    else if (truncation.truncatedBy === "lines") {
                        text += `\n\n[Showing lines ${startLine}-${endLine} of ${truncation.totalLines}. Full output: ${snapshot.fullOutputPath}]`;
                    }
                    else {
                        text += `\n\n[Showing lines ${startLine}-${endLine} of ${truncation.totalLines} (${formatSize(DEFAULT_MAX_BYTES)} limit). Full output: ${snapshot.fullOutputPath}]`;
                    }
                }
                return { text, details };
            };
            const appendStatus = (text, status) => `${text ? `${text}\n\n` : ""}${status}`;
            const startedAt = performance.now();
            try {
                let exitCode;
                try {
                    const result = await ops.exec(spawnContext.command, spawnContext.cwd, {
                        onData: handleData,
                        signal,
                        timeout,
                        env: spawnContext.env,
                    });
                    exitCode = result.exitCode;
                }
                catch (err) {
                    const snapshot = await finishOutput();
                    const { text } = formatOutput(snapshot, "");
                    if (err instanceof Error && err.message === "aborted") {
                        throw new Error(appendStatus(text, "Command aborted"));
                    }
                    if (err instanceof Error && err.message.startsWith("timeout:")) {
                        const timeoutSecs = err.message.split(":")[1];
                        throw new Error(appendStatus(text, `Command timed out after ${timeoutSecs} seconds`));
                    }
                    throw err;
                }
                const snapshot = await finishOutput();
                const { text: outputText, details } = formatOutput(snapshot);
                if (exitCode === null) {
                    throw new Error(appendStatus(outputText, "Command terminated without an exit code"));
                }
                const wallTimeSeconds = Math.round((performance.now() - startedAt) / 100) / 10;
                const fullOutput = await output.readFullOutput(STRUCTURED_OUTPUT_MAX_BYTES);
                const structuredContent = {
                    output: fullOutput.content,
                    truncated: fullOutput.truncated,
                    ...(fullOutput.truncated && snapshot.fullOutputPath
                        ? { full_output_path: snapshot.fullOutputPath }
                        : {}),
                    exit_code: exitCode,
                    wall_time_seconds: wallTimeSeconds,
                };
                if (exitCode !== 0) {
                    return {
                        content: [{ type: "text", text: appendStatus(outputText, `Command exited with code ${exitCode}`) }],
                        details,
                        structuredContent,
                        isError: true,
                    };
                }
                return { content: [{ type: "text", text: outputText }], details, structuredContent };
            }
            finally {
                clearUpdateTimer();
            }
        },
    };
}
const bashToolConfig = {
    name: "bash",
    label: "bash",
    shellName: "bash",
    prompt: "$",
    promptSnippet: bashToolSystemPromptContribution.snippet,
    promptGuidelines: bashToolSystemPromptContribution.guidelines,
    tempFilePrefix: "pi-bash",
};
export function createBashToolDefinition(cwd, options) {
    return createShellToolDefinition(cwd, bashToolConfig, options);
}
