import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

export async function GET() {
  try {
    const { isAuthenticated, userId } = await auth();
    const user = await currentUser();
    const backend_api_key = process.env.BACKEND_API_KEY;

    if (!isAuthenticated || user === null || backend_api_key === undefined)
      throw new Error(`Not authenticated`);

    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
    const newUrl = `${backend_url}/api/v1/repositories/${userId}`;
    console.log("New url: ", newUrl);
    const response = await fetch(newUrl, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${backend_api_key}`,
      },
    });

    if (!response.ok) throw new Error("Failed to get user  repos");

    const result = await response.json();
    console.log("result: ", result);

    return NextResponse.json(result);
  } catch (error) {
    console.log("Error: ", error);
    return NextResponse.json(
      { error: "Internal Server Error" },
      { status: 500 },
    );
  }
}
