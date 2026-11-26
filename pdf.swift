import Foundation
import PDFKit

guard CommandLine.arguments.count == 2,
      let pdf = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else { exit(2) }
let pages = (0..<min(pdf.pageCount, 900)).map { pdf.page(at: $0)?.string ?? "" }
FileHandle.standardOutput.write(try JSONEncoder().encode(pages))
