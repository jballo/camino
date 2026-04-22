import { NextResponse } from "next/server";

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const { url, prompt } = body;

    if (!url || url.length === 0) throw new Error(`Failure to process`);

    console.log("Generating journey for: ", url, " with prompt: ", prompt);
    return new NextResponse(`Success`, { status: 200 });
  } catch (error) {
    console.error("[/api/journeys]: ", error);
    return new NextResponse(`Processing error`, { status: 500 });
  }
}
