const ISSUE_PATH = /^\/([A-Za-z0-9-]{1,39})\/([A-Za-z0-9._-]{1,100})\/issues\/([1-9]\d{0,9})\/?$/;

/**
 * Normalises a GitHub issue URL to `https://github.com/{owner}/{repo}/issues/{n}`,
 * or returns null for anything else (pull requests, other hosts, junk).
 */
export function parseIssueUrl(text: string | null | undefined): string | null {
  if (!text) return null;
  let url: URL;
  try {
    url = new URL(text.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (url.username || url.password || url.port) return null;
  if (url.hostname !== "github.com" && url.hostname !== "www.github.com") return null;

  const match = ISSUE_PATH.exec(url.pathname);
  if (!match) return null;
  const [, owner, repo, number] = match;
  if (repo === "." || repo === "..") return null;
  return `https://github.com/${owner}/${repo}/issues/${number}`;
}
