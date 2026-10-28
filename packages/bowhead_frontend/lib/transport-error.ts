/** Recognize browser transport failures without classifying arbitrary programming errors as network failures. */
export function isNetworkFailure(error: unknown): boolean {
  if (error instanceof DOMException && error.name === "NetworkError") return true;
  return error instanceof TypeError
    && /^(?:failed to fetch|fetch failed|load failed|networkerror(?: when attempting to fetch resource\.?)?|network request failed)$/i.test(error.message.trim());
}
