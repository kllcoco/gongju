// Static decompiler export for a user-supplied binary. Outputs are evidence, not buildable C++.
// @category SnowLumaRecovery
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;
import java.nio.file.*;
import java.io.*;
import java.util.*;

public class ExportRecovery extends GhidraScript {
  private static String clean(String s) { return s.replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n"); }
  public void run() throws Exception {
    Path out = Paths.get(getScriptArgs()[0], currentProgram.getName());
    Files.createDirectories(out.resolve("functions"));
    DecompInterface decomp = new DecompInterface();
    decomp.openProgram(currentProgram);
    int total=0, success=0;
    try (BufferedWriter index=Files.newBufferedWriter(out.resolve("functions.tsv"));
         BufferedWriter all=Files.newBufferedWriter(out.resolve("decompiled.c.txt"));
         BufferedWriter strings=Files.newBufferedWriter(out.resolve("strings-xrefs.tsv"))) {
      index.write("address\tname\tbytes\tdecompiled\tcallees\n");
      all.write("/* Ghidra pseudocode: types/names may be inferred; NOT directly compilable source. */\n");
      FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
      while(functions.hasNext() && !monitor.isCancelled()) {
        Function f=functions.next();if(f.isExternal())continue;total++;
        DecompileResults result=decomp.decompileFunction(f,30,monitor);
        boolean ok=result.decompileCompleted() && result.getDecompiledFunction()!=null;
        String code=ok ? result.getDecompiledFunction().getC() : "/* FAILED: "+result.getErrorMessage()+" */\n";
        if(ok)success++;
        String address=f.getEntryPoint().toString();
        Files.writeString(out.resolve("functions").resolve(address+".c.txt"),code);
        all.write("\n/* ADDRESS "+address+" NAME "+f.getName()+" */\n"+code);
        List<String> calls=new ArrayList<>();
        for(Function called:f.getCalledFunctions(monitor))calls.add(called.getEntryPoint()+":"+called.getName());
        Collections.sort(calls);
        index.write(address+"\t"+clean(f.getName())+"\t"+f.getBody().getNumAddresses()+"\t"+ok+"\t"+clean(String.join(";",calls))+"\n");
        if(total%100==0)println("Exported "+total+" functions from "+currentProgram.getName());
      }
      strings.write("address\tvalue\treferences\n");
      DataIterator data=currentProgram.getListing().getDefinedData(true);
      while(data.hasNext()) {
        Data d=data.next();Object value=d.getValue();if(!(value instanceof String))continue;
        List<String> refs=new ArrayList<>();
        ReferenceIterator it=currentProgram.getReferenceManager().getReferencesTo(d.getAddress());
        while(it.hasNext()) {
          Reference ref=it.next();Function f=currentProgram.getFunctionManager().getFunctionContaining(ref.getFromAddress());
          refs.add(ref.getFromAddress()+":"+(f==null?"data":f.getEntryPoint()+":"+f.getName()));
        }
        strings.write(d.getAddress()+"\t"+clean((String)value)+"\t"+String.join(";",refs)+"\n");
      }
    } finally {decomp.dispose();}
    Files.writeString(out.resolve("summary.json"),"{\"functions\":"+total+",\"decompiled\":"+success+",\"directlyCompilable\":false}\n");
    println("Completed: "+success+"/"+total+" functions in "+out);
  }
}
