import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const body = await req.json();
  const { repoUrl } = body;

  if (repoUrl === null || repoUrl.length === 0)
    throw new NextResponse(`Failure processing ${repoUrl}`, { status: 500 });

  console.log("Respo url: ", repoUrl);
  return new NextResponse(`Sucess processing ${repoUrl}`, { status: 200 });
}
