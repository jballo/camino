import { SignIn } from "@clerk/nextjs";
import { FileCode, Map, Sparkles } from "lucide-react";

const HIGHLIGHTS = [
  {
    icon: FileCode,
    title: "Connect a repository",
    description: "Point Camino at any repo you can access on GitHub.",
  },
  {
    icon: Sparkles,
    title: "Describe a topic",
    description: "Ask how something works and get a grounded walkthrough.",
  },
  {
    icon: Map,
    title: "Follow the tour",
    description: "Step through an ordered, code-linked explanation.",
  },
];

function safeRedirect(value: string | string[] | undefined): string {
  const raw = Array.isArray(value) ? value[0] : value;
  // Only allow same-origin relative paths to avoid open redirects.
  if (raw && raw.startsWith("/") && !raw.startsWith("//")) return raw;
  return "/";
}

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ redirect_url?: string | string[] }>;
}) {
  const { redirect_url } = await searchParams;
  const fallbackRedirectUrl = safeRedirect(redirect_url);

  return (
    <div className="flex min-h-full w-full items-center justify-center px-8 py-12">
      <div className="grid w-full max-w-4xl items-center gap-12 lg:grid-cols-2">
        <div className="flex flex-col gap-8">
          <div className="flex flex-col gap-3">
            <h1 className="text-3xl font-semibold">Welcome to Camino</h1>
            <p className="max-w-md text-sm text-muted-foreground">
              Guided tours of unfamiliar codebases. Sign in to connect a
              repository and start building code-grounded walkthroughs.
            </p>
          </div>

          <ul className="flex flex-col gap-5">
            {HIGHLIGHTS.map(({ icon: Icon, title, description }) => (
              <li key={title} className="flex items-start gap-3">
                <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-accent">
                  <Icon className="size-4 text-primary" />
                </span>
                <div className="flex flex-col gap-0.5">
                  <span className="text-sm font-medium">{title}</span>
                  <span className="text-sm text-muted-foreground">
                    {description}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <div className="flex justify-center lg:justify-end">
          <SignIn fallbackRedirectUrl={fallbackRedirectUrl} />
        </div>
      </div>
    </div>
  );
}
