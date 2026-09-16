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
