import pdfkit
import mistune

# Custom rendering for table
class DivRenderer(mistune.HTMLRenderer):
    def table(self, header, body):
        return f'<div class="table-wrapper"><div class="thead">{header}</div><div class="tbody">{body}</div></div>'

    def table_row(self, content):
        return f'<div class="row">{content}</div>'

    def table_cell(self, content, **flags):
        return f'<div class="cell">{content}</div>'

# Convert markdown to HTML with custom table rendering
def md_to_html(text):
    renderer = DivRenderer()
    parser = mistune.create_markdown(renderer=renderer, plugins=['table'])
    return parser(text)

def TicketPDF(issue, vault_url, jira, filekey, field_mapping=None, project_custom_fields=None):
    """
    Prepare a PDF summary of a ticket
    
    Args:
        issue: The Jira issue object with navigable fields
        vault_url: The URL of the Jira vault
        jira: The Jira client
        filekey: The filename key for the PDF
        field_mapping: Optional mapping of field IDs to display names
        project_custom_fields: Optional dictionary of project-specific custom fields
        
    Returns:
        str: The path to the generated PDF file
    """

    fields = issue['fields']
    playertoken = issue.get('EXTRACTED_IDENTIFIER')
    key = issue.get('key')
    link = f"{vault_url}/browse/{key}"
    attachments = jira.get_attachments_ids_from_issue(key)
    worklogs = fields.get('worklog',[])  
    changelog = issue.get('changelog',[])
    comments = fields.get('comment',[])

    # Create the ticket summary in HTML table format with border and outline
    ticket_summary = f'''
    <!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Report</title>
<style>
body {{
font-family: Arial, sans-serif;
}}
table {{
border-collapse: collapse;
width: 100%;
margin-bottom: 20px;
}}
th, td {{
border: 1px solid #dee2e6;
text-align: left;
padding: 8px;
}}
th {{
background-color: #f8f9fa;
font-weight: bold;
}}
h2 {{
margin-bottom: 5px;
}}
</style>
</head>
<body>
<table>
<tr>
<th>
Extracted Identifier:
</th>
<td>
{playertoken}
</td>
</tr>
<tr>
<th>
Jira Key:
</th>
<td>
{key}
</td>
</tr>
<tr>
<th>Link</th>
<td><a href="{link}">{link}</a></td>
</tr>
{
    "".join(
        f"<tr><th>{field_mapping.get(key, key) if field_mapping else key}</th><td>{str(value)}</td></tr>" 
        for key, value in fields.items()
    )
}
<tr>
<th>
Resolution
</th>
<td>
{fields['resolution']['name'] if fields.get('resolution') else "None"}
</td>
</tr>
</table>
<h2>Attachments</h2>
<table>
<tr>
<th>Filename</th>
</tr>
{"".join([f"<tr><td>{file['filename']}</td></tr>" for file in attachments]) if attachments else "<tr><td>No attachments available</td></tr>"}
</table>

<h2>Comments</h2>
<table>
<tr>
<th>Author</th>
<th>Comment</th>
<th>Created</th>
<th>Updated</th>
</tr>
{"".join([f"<tr><td>{comment['author']['displayName']} </td><td>{md_to_html(comment['body'])}</td><td>{comment['created']}</td><td>{comment['updated']}</td></tr>" for comment in comments]) if comments else "<tr><td colspan='4'>No comments available</td></tr>"}
</table>

<h2>Changelog</h2>
<table>
<tr>
<th>Author</th>
<th>Created</th>
<th>Change Items</th>
</tr>
{"".join(["<tr><td>" + log['author']['displayName'] + "</td><td>" + log['created'] + "</td><td>" + ",".join([
'Field: ' + str(item['field']) + ', FieldType: ' + str(item['fieldtype']) + ', From: ' + str(item.get('from', 'N/A')) + ', FromString: ' +
str(item.get('fromString', 'N/A')) + ', To: ' + str(item.get('to', 'N/A')) + ', ToString: ' + str(item.get('toString', 'N/A')) + chr(10)
for item in log['items']]) + "</td></tr>" for log in changelog]) if changelog else "<tr><td colspan='3'>No changelog available</td></tr>"}
</table>
    '''
    ticket_summary + f'''
    <h2>Worklog</h2>
<table>
<tr>
<th>Author</th>
<th>Comment</th>
<th>Date</th>
<th>Timespent</th>
</tr>
{"".join([f"<tr><td>{log['author']['displayName']}</td><td>{log['comment']}</td><td>{log['created']}</td><td>{log['timeSpent']}</td></tr>" for log in worklogs])}
</table>
''' if worklogs else ticket_summary + "<h2>No Worklog Available</h2>"


    options = {
        'encoding': "UTF-8"
    }


    pdfoutput = f'./{filekey}/{filekey}.pdf'

    pdfkit.from_string(str(ticket_summary), pdfoutput, options=options)



    return pdfoutput
