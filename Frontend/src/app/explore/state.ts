import { ApiError } from "../../lib/api";

type AnswerSelection = {
  repoName: string;
  ref: string;
};

export function repositorySelectionChanged(
  previousRepo: string | undefined,
  nextRepo: string | undefined,
) {
  return previousRepo !== undefined && previousRepo !== nextRepo;
}

export function answerMatchesSelection(
  answer: AnswerSelection | undefined,
  repoName: string | undefined,
  ref: string | undefined,
) {
  return (
    answer !== undefined && answer.repoName === repoName && answer.ref === ref
  );
}

function retryWait(seconds: number | undefined) {
  if (seconds === undefined) return "later";
  const minutes = Math.ceil(seconds / 60);
  return minutes <= 1 ? "in about a minute" : `in about ${minutes} minutes`;
}

export function followErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 404) {
    return "Repository not found — or it is private and unavailable to the GitHub App.";
  }
  if (error instanceof ApiError && error.status === 502) {
    return "GitHub check failed — try again.";
  }
  if (error instanceof ApiError && error.status === 429) {
    return `You've queued as many new repositories for indexing as you can for now. Try again ${retryWait(error.retryAfterSeconds)}.`;
  }
  return "Could not add that repository.";
}
