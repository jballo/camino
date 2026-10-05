import BriefWorkbench from "@/components/brief-workbench";
import { parseIssueUrl } from "@/lib/issue-url";

export default async function BriefsPage({
  searchParams,
}: {
  searchParams: Promise<{ issue?: string | string[] }>;
}) {
  const { issue } = await searchParams;
  // Only a well-formed GitHub issue URL is handed on, and only to the
  // read-only preview. Generating a brief always needs a click.
  const initialIssueUrl = parseIssueUrl(Array.isArray(issue) ? issue[0] : issue) ?? undefined;
  return <BriefWorkbench initialIssueUrl={initialIssueUrl} />;
}
