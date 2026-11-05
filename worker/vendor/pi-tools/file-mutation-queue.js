// Derived from @earendil-works/pi-coding-agent v1.5.0 (MIT). See provenance.json and LICENSE.
import { resolve } from "./remote-path.js";
const fileMutationQueues = new Map();
let registrationQueue = Promise.resolve();
function isMissingPathError(error) {
    return (typeof error === "object" &&
        error !== null &&
        "code" in error &&
        (error.code === "ENOENT" || error.code === "ENOTDIR"));
}
async function getMutationQueueKey(filePath) {
    return resolve(filePath);
}
/**
 * Serialize file mutation operations targeting the same file.
 * Operations for different files still run in parallel.
 */
export async function withFileMutationQueue(filePath, fn) {
    const registration = registrationQueue.then(async () => {
        const key = await getMutationQueueKey(filePath);
        const currentQueue = fileMutationQueues.get(key) ?? Promise.resolve();
        let releaseNext;
        const nextQueue = new Promise((resolveQueue) => {
            releaseNext = resolveQueue;
        });
        const chainedQueue = currentQueue.then(() => nextQueue);
        fileMutationQueues.set(key, chainedQueue);
        return { key, currentQueue, chainedQueue, releaseNext };
    });
    registrationQueue = registration.then(() => undefined, () => undefined);
    const { key, currentQueue, chainedQueue, releaseNext } = await registration;
    await currentQueue;
    try {
        return await fn();
    }
    finally {
        releaseNext();
        if (fileMutationQueues.get(key) === chainedQueue) {
            fileMutationQueues.delete(key);
        }
    }
}
