import sys
if '--mcp' in sys.argv:
    sys.argv.remove('--mcp')
    from mcp_server import main
else:
    from desktop_app import main
if __name__=='__main__':main()
