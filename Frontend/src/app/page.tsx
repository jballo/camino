import Header from "@/components/header";
import { Button, Textarea } from "@headlessui/react";
import { ArrowUp } from "lucide-react";

export default function Home() {
  return (
    <div className="flex flex-col w-full h-screen">
      <div className="flex w-full h-1/12">
        <Header />
      </div>
      <div className="flex flex-col justify-center items-center w-full h-11/12">
        <div className="flex flex-col justify-center items-center w-2/3 h-1/4 gap-4 -mt-40">
          <div className="flex justify-center">
            <h2 className="text-4xl">Peep into a Codebase</h2>
          </div>
          <div className="flex flex-col outline-1 outline-accent rounded-2xl p-5 gap-7 w-full max-w-[1000px]">
            <Textarea
              placeholder="What do you want to know?"
              className="w-full text-start focus:outline-none field-sizing-content max-h-40 resize-none"
            />
            <div className="flex w-full justify-between">
              <p>(:</p>
              <Button
                className="flex w-7 h-7 bg-primary rounded-md justify-center items-center"
                aria-label="Submit"
              >
                <div>
                  <ArrowUp aria-hidden="true" />
                </div>
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
