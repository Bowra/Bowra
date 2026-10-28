/**
 * Asymmetric matcher for request headers sent through appFetch.
 *
 * Since d59ce0c12 appFetch sends same-origin requests with a Headers instance that
 * always carries X-Bowhead-Public-Base. The matcher requires exactly the expected
 * header names plus that public-base header, with matching values, so an extra or
 * missing header fails the assertion.
 */
export function headersExactly(
  expected: Record<string, string>,
  publicBase: string = window.location.origin,
) {
  const want = new Headers(expected);
  want.set("X-Bowhead-Public-Base", publicBase);
  const wantEntries = [...want.entries()].sort(([a], [b]) => a.localeCompare(b));
  return {
    asymmetricMatch: (actual: HeadersInit | undefined) => {
      if (actual === undefined) return false;
      const got = [...new Headers(actual).entries()].sort(([a], [b]) => a.localeCompare(b));
      return JSON.stringify(got) === JSON.stringify(wantEntries);
    },
    toAsymmetricMatcher: () => `HeadersExactly ${JSON.stringify(Object.fromEntries(wantEntries))}`,
  };
}
