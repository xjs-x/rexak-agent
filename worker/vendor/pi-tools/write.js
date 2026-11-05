// Derived from @earendil-works/pi-coding-agent v1.5.0 (MIT). See provenance.json and LICENSE.
import { dirname } from "./remote-path.js";
import { Type } from "typebox";
import { withFileMutationQueue } from "./file-mutation-queue.js";
import { resolveToCwd } from "./remote-path.js";
const writeSchema = Type.Object({
    path: Type.String({ description: "Path to the file to write (relative or absolute)" }),
    content: Type.String({ description: "Content to write to the file" }),
});
export const writeToolSystemPromptContribution = {
    snippet: "Create or overwrite files",
    guidelines: ["Use write only for new files or complete rewrites."],
};
export function createWriteToolDefinition(cwd, options) {
    const ops = options.operations;
    return {
        name: "write",
        label: "write",
        description: "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories.",
        promptSnippet: writeToolSystemPromptContribution.snippet,
        promptGuidelines: [...writeToolSystemPromptContribution.guidelines],
        parameters: writeSchema,
        constrainedSampling: { type: "json_schema", strict: "prefer" },
        async execute(_toolCallId, { path, content }, signal, _onUpdate, ctx) {
            const absolutePath = resolveToCwd(path, ctx?.cwd || cwd);
            const dir = dirname(absolutePath);
            return withFileMutationQueue(absolutePath, async () => {
                // Do not reject from an abort event listener here: that would release the
                // mutation queue while an in-flight filesystem operation may still finish.
                // Checking signal.aborted after each await observes the same aborts while
                // keeping the queue locked until the current operation has settled.
                const throwIfAborted = () => {
                    if (signal?.aborted)
                        throw new Error("Operation aborted");
                };
                throwIfAborted();
                // Create parent directories if needed.
                await ops.mkdir(dir);
                throwIfAborted();
                // Write the file contents.
                await ops.writeFile(absolutePath, content);
                throwIfAborted();
                return {
                    content: [{ type: "text", text: `Successfully wrote to ${path}` }],
                    details: undefined,
                };
            });
        },
    };
}
