import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const body = await req.json();
  const { repoUrl, prompt } = body;

  if (repoUrl === null || repoUrl.length === 0)
    throw new NextResponse(`Failure to process`, { status: 500 });

  console.log("Generating journey for: ", repoUrl, " with prompt: ", prompt);
  return new NextResponse(`Sucess`, { status: 200 });
}
